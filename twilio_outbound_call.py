#!/usr/bin/env python3
"""
Scam Call Console: Outbound caller service with admin UI, call pacing, and Twilio integration.

Features in this build
- /scamcalls UI with:
  - "Call now" button
  - "Add greeting phrase" button (client and server enforce 5–15 words)
  - Admin button (login/logout)
  - Matrix background (static assets)
  - Admin panel (when logged in) to edit non-secret .env keys
- Admin authentication
  - Defaults to username "bootycall" and password "scammers" if .env ADMIN_* is absent
  - If .env contains ADMIN_USER and ADMIN_PASSWORD_HASH, bcrypt verification is used
- .env editing (non-secret keys only)
  - GET /api/admin/env returns editable keys
  - POST /api/admin/env persists updates back to the .env file and refreshes runtime settings
- "Call now"
  - POST /api/call-now requests an immediate attempt (respects active window and caps)
  - Returns HTTP 429 with {"ok": false, "reason": "cap_reached"} when max attempts reached in allotted time
- One-time greeting phrase
  - POST /api/next-greeting sets a one-shot phrase applied to the next call, then cleared
- Outbound dialer
  - Background scheduler attempts calls at randomized intervals between MIN/MAX interval seconds
  - Uses FROM_NUMBERS (random pick per attempt) when provided; else uses FROM_NUMBER
  - Enforces HOURLY_MAX_ATTEMPTS_PER_DEST and DAILY_MAX_ATTEMPTS_PER_DEST
  - Active hours/days enforcement

Notes
- Keep FLASK_SECRET set in .env for production usage.
- Twilio credentials (ACCOUNT_SID, AUTH_TOKEN) are expected in real env (process env or hosting configuration),
  and are intentionally not part of the editable .env keys in the admin UI.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import atexit
import queue
import random
import signal
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    Response,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# Optional dependencies
try:
    import bcrypt  # used when ADMIN_PASSWORD_HASH is configured
except Exception:
    bcrypt = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# Twilio SDK
try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
except Exception:
    Client = None
    TwilioRestException = Exception  # type: ignore


# -----------------------------------------------------------------------------
# Environment and configuration
# -----------------------------------------------------------------------------

DOTENV_PATH = os.environ.get("DOTENV_PATH") or ".env"
if load_dotenv:
    # Load early to populate os.environ for configuration parsing.
    load_dotenv(DOTENV_PATH)

# Application
app = Flask(__name__, static_folder="static", template_folder="templates")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)  # honor reverse proxy headers
app.secret_key = os.environ.get("FLASK_SECRET", "dev_insecure_change_me")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Globals managed at runtime
_runtime_lock = threading.Lock()
_manual_call_requested = threading.Event()
_stop_requested = threading.Event()

# One-shot greeting phrase (next call only)
_ONE_SHOT_GREETING = None
_ONE_SHOT_GREETING_LOCK = threading.Lock()

# Attempt tracking per-destination
_attempts_lock = threading.Lock()
_dest_attempts: Dict[str, List[float]] = {}  # to_number -> epoch list

# Next randomized interval bookkeeping (for UI status)
_next_call_epoch_s_lock = threading.Lock()
_next_call_epoch_s: Optional[int] = None
# Track interval timing to enable progress visualization in UI
_interval_start_epoch_s: Optional[int] = None
_interval_total_seconds: Optional[int] = None

# Twilio client
_twilio_client: Optional[Client] = None


# -----------------------------------------------------------------------------
# Configuration handling
# -----------------------------------------------------------------------------

# Keys observed in .env (names only). Values may contain potentially sensitive data; do not log values.
def _load_dotenv_pairs(path: str) -> List[Tuple[str, str]]:
    """
    Reads KEY=VALUE lines from a .env-like file. Preserves only simple key/value lines.
    Comments and blank lines are ignored in the returned list, but retained during writes.
    """
    pairs: List[Tuple[str, str]] = []
    if not os.path.exists(path):
        return pairs
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line or line.lstrip().startswith("#"):
                    continue
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
                if not m:
                    continue
                key = m.group(1)
                val = m.group(2)
                # Remove optional surrounding quotes (simple heuristic)
                if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                    val = val[1:-1]
                pairs.append((key, val))
    except Exception as e:
        logging.error("Failed to read .env pairs: %s", e)
    return pairs


def _load_dotenv_lines(path: str) -> List[str]:
    """Returns all lines for write-back with minimal churn."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except Exception:
        return []


def _write_dotenv_lines(path: str, lines: List[str]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines and not lines[-1].endswith("\n") else ""))
    os.replace(tmp, path)


def _sanitize_env_value(v: str) -> str:
    v = v.replace("\r", "").replace("\n", "")
    # Quote values containing spaces or special characters
    if re.search(r"\s|#|\"|'", v):
        # Escape internal quotes
        vv = v.replace('"', '\\"')
        return f"\"{vv}\""
    return v


def _secrets_denylist() -> List[re.Pattern]:
    """
    Denylist patterns for secret-like names. Case-insensitive.
    Includes specific names plus generic suffix/prefix checks.
    """
    patterns = [
        re.compile(r".*TOKEN.*", re.I),
        re.compile(r".*SECRET.*", re.I),
        re.compile(r".*PASSWORD.*", re.I),
        re.compile(r".*AUTH.*", re.I),
        re.compile(r".*ACCOUNT_SID.*", re.I),
        re.compile(r".*API[_-]?KEY.*", re.I),
        re.compile(r".*PRIVATE[_-]?KEY.*", re.I),
    ]
    # Specific ones we always exclude
    specifics = [
        "NGROK_AUTHTOKEN",
        "FLASK_SECRET",
        "ADMIN_PASSWORD_HASH",
    ]
    for s in specifics:
        patterns.append(re.compile(rf"^{re.escape(s)}$", re.I))
    return patterns


def _is_secret_key(name: str) -> bool:
    for pat in _secrets_denylist():
        if pat.match(name):
            return True
    return False


def _current_env_editable_pairs() -> List[Tuple[str, str]]:
    pairs = _load_dotenv_pairs(DOTENV_PATH)
    editable = []
    for k, v in pairs:
        if _is_secret_key(k):
            continue
        # Optionally avoid allowing admin username changes via UI to prevent lockouts
        if k.upper() in {"ADMIN_USER"}:
            continue
        editable.append((k, v))
    # Keep stable ordering (alpha by key)
    editable.sort(key=lambda kv: kv[0])
    return editable


def _apply_env_updates(updates: Dict[str, str]) -> None:
    """
    Apply updates to .env file for non-secret keys only, then refresh process env and runtime config.
    """
    lines = _load_dotenv_lines(DOTENV_PATH)
    if not lines:
        # Create new .env if needed
        lines = []
    existing_keys = {}
    for idx, raw in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", raw)
        if m:
            existing_keys[m.group(1)] = idx

    for key, new_val in updates.items():
        if _is_secret_key(key) or key.upper() == "ADMIN_USER":
            continue
        sval = _sanitize_env_value(str(new_val))
        if key in existing_keys:
            idx = existing_keys[key]
            # Preserve comment indentation if present
            prefix_ws = ""
            m = re.match(r"^(\s*)", lines[idx])
            if m:
                prefix_ws = m.group(1)
            lines[idx] = f"{prefix_ws}{key}={sval}"
        else:
            lines.append(f"{key}={sval}")

        # Update process environment for immediate effect (best-effort)
        os.environ[key] = str(new_val)

    _write_dotenv_lines(DOTENV_PATH, lines)
    # Reload runtime config dependent values
    _reload_runtime_from_env()


# -----------------------------------------------------------------------------
# Runtime config values and helpers
# -----------------------------------------------------------------------------

def _parse_bool(s: Optional[str], default: bool = False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(s: Optional[str], default: int) -> int:
    if s is None:
        return default
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _parse_csv(s: Optional[str]) -> List[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


class Runtime:
    def __init__(self) -> None:
        # Numbers (E.164)
        self.to_number = os.environ.get("TO_NUMBER", "").strip()
        self.from_number = os.environ.get("FROM_NUMBER", "").strip()
        self.from_numbers = _parse_csv(os.environ.get("FROM_NUMBERS"))

        # Schedule and attempt limits
        self.active_hours_local = os.environ.get("ACTIVE_HOURS_LOCAL", "09:00-18:00").strip()
        self.active_days = [d.strip().title() for d in _parse_csv(os.environ.get("ACTIVE_DAYS") or "Mon,Tue,Wed,Thu,Fri")]
        self.min_interval_seconds = max(30, _parse_int(os.environ.get("MIN_INTERVAL_SECONDS"), 120))
        self.max_interval_seconds = max(self.min_interval_seconds, _parse_int(os.environ.get("MAX_INTERVAL_SECONDS"), 420))
        self.hourly_max_attempts = max(1, _parse_int(os.environ.get("HOURLY_MAX_ATTEMPTS_PER_DEST"), 3))
        self.daily_max_attempts = max(1, _parse_int(os.environ.get("DAILY_MAX_ATTEMPTS_PER_DEST"), 12))
        self.backoff_strategy = (os.environ.get("BACKOFF_STRATEGY", "none") or "none").strip().lower()

        # Jurisdiction / recording (surface only, call flow minimal here)
        self.recording_mode = (os.environ.get("RECORDING_MODE", "off") or "off").strip().lower()

        # Twilio and webhooks
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
        self.use_ngrok = _parse_bool(os.environ.get("USE_NGROK"), False)

        # Content
        self.company_name = os.environ.get("COMPANY_NAME", "Your Company").strip() or "Your Company"
        self.topic = os.environ.get("TOPIC", "availability").strip() or "availability"
        self.tts_voice = os.environ.get("TTS_VOICE", "man").strip() or "man"
        self.tts_language = os.environ.get("TTS_LANGUAGE", "en-US").strip() or "en-US"

        # Admin defaults
        self.admin_user = os.environ.get("ADMIN_USER") or None
        self.admin_password_hash = os.environ.get("ADMIN_PASSWORD_HASH") or None

        # AMD (acknowledged, not fully implemented)
        self.amd_mode = (os.environ.get("AMD_MODE", "off") or "off").strip().lower()
        self.amd_timeout_seconds = max(3, _parse_int(os.environ.get("AMD_TIMEOUT_SECONDS"), 8))
        self.machine_behavior = (os.environ.get("MACHINE_BEHAVIOR", "hangup") or "hangup").strip().lower()

        # Silence hangup (not used in this minimal flow)
        self.callee_silence_hangup_seconds = max(5, min(60, _parse_int(os.environ.get("CALLEE_SILENCE_HANGUP_SECONDS"), 10)))


_runtime = Runtime()


def _reload_runtime_from_env() -> None:
    global _runtime
    with _runtime_lock:
        _runtime = Runtime()
        logging.info("Runtime configuration refreshed from environment.")


def _now_local() -> datetime:
    return datetime.now()


def _parse_active_window(active_str: str) -> Tuple[int, int]:
    try:
        s, e = active_str.split("-")
        hs, ms = [int(x) for x in s.strip().split(":")]
        he, me = [int(x) for x in e.strip().split(":")]
        return hs * 60 + ms, he * 60 + me
    except Exception:
        return 9 * 60, 18 * 60  # default 09:00-18:00


def _within_active_window(now_dt: datetime) -> bool:
    day = now_dt.strftime("%a")
    if day not in _runtime.active_days:
        return False
    start_min, end_min = _parse_active_window(_runtime.active_hours_local)
    mins = now_dt.hour * 60 + now_dt.minute
    if start_min <= end_min:
        return start_min <= mins < end_min
    # Overnight window case
    return mins >= start_min or mins < end_min


def _prune_attempts(now_ts: float, to_number: str) -> None:
    with _attempts_lock:
        prev = _dest_attempts.get(to_number, [])
        cutoff = now_ts - 86400
        _dest_attempts[to_number] = [t for t in prev if t >= cutoff]


def _can_attempt(now_ts: float, to_number: str) -> Tuple[bool, int]:
    """
    Returns (allowed, wait_seconds).
    wait_seconds > 0 indicates the time until a new attempt is allowed.
    """
    _prune_attempts(now_ts, to_number)
    with _attempts_lock:
        lst = _dest_attempts.get(to_number, [])
        last_hour = [t for t in lst if t >= now_ts - 3600]
        last_day = lst
        hourly_ok = len(last_hour) < _runtime.hourly_max_attempts
        daily_ok = len(last_day) < _runtime.daily_max_attempts

        wait_hour = 0
        wait_day = 0
        if not hourly_ok and last_hour:
            next_allowed_hour = min(last_hour) + 3600
            wait_hour = max(0, int(next_allowed_hour - now_ts))
        if not daily_ok and last_day:
            next_allowed_day = min(last_day) + 86400
            wait_day = max(0, int(next_allowed_day - now_ts))

        allowed = hourly_ok and daily_ok
        wait_total = max(wait_hour, wait_day)
        return allowed, wait_total


def _note_attempt(now_ts: float, to_number: str) -> None:
    with _attempts_lock:
        _dest_attempts.setdefault(to_number, []).append(now_ts)


def _choose_from_number() -> Optional[str]:
    if _runtime.from_numbers:
        return random.choice(_runtime.from_numbers)
    return _runtime.from_number or None


# -----------------------------------------------------------------------------
# Twilio integration
# -----------------------------------------------------------------------------

def _ensure_twilio_client() -> Optional[Client]:
    global _twilio_client
    if _twilio_client is not None:
        return _twilio_client
    if Client is None:
        logging.error("Twilio SDK not available. Install with: pip install twilio")
        return None

    # Twilio credentials should be in real environment (not editable via UI)
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logging.error("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in environment.")
        return None
    _twilio_client = Client(account_sid, auth_token)
    return _twilio_client


def initiate_outbound_call() -> Tuple[bool, str]:
    """
    Create an outbound call via Twilio. Returns (ok, message).
    """
    client = _ensure_twilio_client()
    if client is None:
        return False, "Twilio client is not configured."

    if not _runtime.public_base_url:
        return False, "PUBLIC_BASE_URL is not configured."

    to_number = _runtime.to_number
    if not to_number:
        return False, "TO_NUMBER is not configured."

    from_number = _choose_from_number()
    if not from_number:
        return False, "No FROM_NUMBER or FROM_NUMBERS configured."

    answer_url = f"{_runtime.public_base_url.rstrip('/')}/twilio/answer"
    try:
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            url=answer_url,
            machine_detection="DetectMessageEnd" if _runtime.amd_mode in {"detect", "detect_hangup", "detect_message"} else None,
            time_limit=300,  # upper bound; practical flow is short in this build
        )
        _note_attempt(time.time(), to_number)
        logging.info("Outbound call created. CallSid=%s to=%s from=%s", getattr(call, "sid", "?"), to_number, from_number)
        return True, "Call initiated."
    except TwilioRestException as e:
        logging.error("Twilio error while initiating call: %s", e)
        return False, f"Twilio error: {e}"
    except Exception as e:
        logging.error("Error while initiating call: %s", e)
        return False, f"Error: {e}"


# -----------------------------------------------------------------------------
# Background scheduler
# -----------------------------------------------------------------------------

def _rand_interval_seconds() -> int:
    lo = _runtime.min_interval_seconds
    hi = _runtime.max_interval_seconds
    if hi <= lo:
        return lo
    return random.randint(lo, hi)


def _set_next_call_epoch(delta_s: int) -> None:
    global _next_call_epoch_s, _interval_total_seconds, _interval_start_epoch_s
    now_i = int(time.time())
    with _next_call_epoch_s_lock:
        _next_call_epoch_s = now_i + max(0, int(delta_s))
        _interval_total_seconds = max(0, int(delta_s))
        _interval_start_epoch_s = now_i


def _dialer_loop() -> None:
    logging.info("Dialer loop started.")
    delay_s = _rand_interval_seconds()
    _set_next_call_epoch(delay_s)

    while not _stop_requested.is_set():
        # Wake up early if "call now" was requested.
        if _manual_call_requested.wait(timeout=1.0):
            _manual_call_requested.clear()
            # Attempt immediately if within active window and caps permit.
            now = _now_local()
            if not _within_active_window(now):
                logging.info("Call now rejected: outside active window.")
            else:
                allowed, wait_s = _can_attempt(time.time(), _runtime.to_number)
                if not allowed:
                    logging.info("Call now rejected: cap reached, wait %ss.", wait_s)
                else:
                    ok, msg = initiate_outbound_call()
                    logging.info("Call now attempt: %s", msg)
            # Re-arm next randomized delay after manual action
            delay_s = _rand_interval_seconds()
            _set_next_call_epoch(delay_s)
            continue

        # Periodic check once per second
        now_ts = int(time.time())
        with _next_call_epoch_s_lock:
            due = _next_call_epoch_s is not None and now_ts >= _next_call_epoch_s

        if not due:
            continue

        now_local = _now_local()
        if not _within_active_window(now_local):
            # Reschedule to next active window boundary
            delay_s = 60  # check again shortly
            _set_next_call_epoch(delay_s)
            continue

        allowed, wait_s = _can_attempt(time.time(), _runtime.to_number)
        if not allowed:
            # Respect caps; schedule another check after the wait or a minute if unknown
            delay_s = max(wait_s, 60)
            _set_next_call_epoch(delay_s)
            continue

        ok, msg = initiate_outbound_call()
        logging.info("Scheduled attempt: %s", msg)

        # Reschedule next randomized interval
        delay_s = _rand_interval_seconds()
        _set_next_call_epoch(delay_s)

    logging.info("Dialer loop stopped.")


_dialer_thread = threading.Thread(target=_dialer_loop, name="dialer", daemon=True)


# -----------------------------------------------------------------------------
# Admin authentication helpers
# -----------------------------------------------------------------------------

def _admin_defaults() -> Tuple[str, Optional[str], bool]:
    """
    Returns (effective_user, effective_hash_or_none, uses_hash).
    If .env ADMIN_USER and ADMIN_PASSWORD_HASH exist (and bcrypt available), prefer them.
    Otherwise fall back to "bootycall"/"scammers" with no hash.
    """
    env_user = (_runtime.admin_user or "").strip() if _runtime.admin_user else None
    env_hash = (_runtime.admin_password_hash or "").strip() if _runtime.admin_password_hash else None
    if env_user and env_hash and bcrypt is not None:
        return env_user, env_hash, True
    # Default static credentials as requested
    return "bootycall", None, False


def _admin_authenticated() -> bool:
    return bool(session.get("is_admin") is True)


def _require_admin() -> Optional[Response]:
    if not _admin_authenticated():
        return redirect(url_for("admin_login"))
    return None


# -----------------------------------------------------------------------------
# Flask routes: UI and API
# -----------------------------------------------------------------------------

@app.route("/")
def root():
    return redirect(url_for("scamcalls"))


@app.route("/scamcalls", methods=["GET"])
def scamcalls():
    return render_template("scamcalls.html", is_admin=_admin_authenticated())


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        if _admin_authenticated():
            return redirect(url_for("scamcalls"))
        return render_template("admin_login.html", error=None)

    # POST
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    effective_user, effective_hash, uses_hash = _admin_defaults()
    ok = False
    if uses_hash and effective_hash and bcrypt is not None:
        if username == effective_user:
            try:
                ok = bcrypt.checkpw(password.encode("utf-8"), effective_hash.encode("utf-8"))
            except Exception:
                ok = False
    else:
        # Fallback: "bootycall" / "scammers"
        ok = (username == effective_user and password == "scammers")

    if not ok:
        return render_template("admin_login.html", error="Invalid credentials.")

    session["is_admin"] = True
    return redirect(url_for("scamcalls"))


@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("scamcalls"))


@app.route("/api/call-now", methods=["POST"])
def api_call_now():
    # Enforce caps and active window
    now_local = _now_local()
    if not _within_active_window(now_local):
        # Allow silent rejection or a message. Here we return 200 with info to keep UI simple.
        return jsonify(ok=False, reason="outside_active_window", message="Outside active calling window."), 200

    allowed, wait_s = _can_attempt(time.time(), _runtime.to_number)
    if not allowed:
        return jsonify(ok=False, reason="cap_reached", wait_seconds=wait_s), 429

    # Signal the dialer loop to attempt immediately
    _manual_call_requested.set()
    return jsonify(ok=True)


@app.route("/api/next-greeting", methods=["POST"])
def api_next_greeting():
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    phrase = (data.get("phrase") or "").strip()

    # Validate 5–15 words
    words = [w for w in re.split(r"\s+", phrase) if w]
    if not (5 <= len(words) <= 15):
        return Response("Phrase must be between 5 and 15 words.", status=400)

    global _ONE_SHOT_GREETING
    with _ONE_SHOT_GREETING_LOCK:
        _ONE_SHOT_GREETING = phrase
    return jsonify(ok=True)


@app.route("/api/status", methods=["GET"])
def api_status():
    """
    Provide scheduling and pacing status for the UI countdown.
    Returns JSON with timing and cap information.
    """
    now_i = int(time.time())
    with _next_call_epoch_s_lock:
        next_epoch = _next_call_epoch_s
        interval_start = _interval_start_epoch_s
        interval_total = _interval_total_seconds

    within = _within_active_window(_now_local())

    to = _runtime.to_number
    attempts_last_hour = 0
    attempts_last_day = 0
    can_attempt = True
    wait_if_capped = 0
    if to:
        _prune_attempts(now_i, to)
        with _attempts_lock:
            lst = _dest_attempts.get(to, [])
            attempts_last_hour = len([t for t in lst if t >= now_i - 3600])
            attempts_last_day = len(lst)
        can_attempt, wait_if_capped = _can_attempt(now_i, to)

    seconds_until_next = None
    if next_epoch is not None:
        seconds_until_next = max(0, int(next_epoch - now_i))

    interval_elapsed = None
    if interval_start is not None and interval_total is not None:
        interval_elapsed = max(0, now_i - interval_start)

    payload = {
        "now_epoch": now_i,
        "next_call_epoch": next_epoch,
        "seconds_until_next": seconds_until_next,
        "within_active_window": within,
        "active_hours_local": _runtime.active_hours_local,
        "active_days": _runtime.active_days,
        "attempts_last_hour": attempts_last_hour,
        "attempts_last_day": attempts_last_day,
        "hourly_max_attempts": _runtime.hourly_max_attempts,
        "daily_max_attempts": _runtime.daily_max_attempts,
        "can_attempt_now": can_attempt,
        "wait_seconds_if_capped": wait_if_capped,
        "interval_total_seconds": interval_total,
        "interval_elapsed_seconds": interval_elapsed,
    }
    return jsonify(payload)


# -----------------------------------------------------------------------------
# Twilio webhook routes
# -----------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@app.route("/twilio/answer", methods=["POST", "GET"])
def twilio_answer():
    """
    Basic TwiML to speak one-time greeting (if provided) and a short default line.
    This is intentionally simple to keep the call brief for this build.
    """
    phrase = None
    global _ONE_SHOT_GREETING
    with _ONE_SHOT_GREETING_LOCK:
        if _ONE_SHOT_GREETING:
            phrase = _ONE_SHOT_GREETING
            _ONE_SHOT_GREETING = None

    greeting_lines: List[str] = []
    if phrase:
        greeting_lines.append(phrase)
    else:
        # Default opening if no one-shot phrase is set
        greeting_lines.append(
            f"Hello, this is { _xml_escape(_runtime.company_name) }. I am calling about { _xml_escape(_runtime.topic) }."
        )

    # Keep the response concise; in practice you may extend this with <Gather> etc.
    say_text = " ".join(greeting_lines)
    voice = _runtime.tts_voice or "man"
    lang = _runtime.tts_language or "en-US"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="{_xml_escape(voice)}" language="{_xml_escape(lang)}">{_xml_escape(say_text)}</Say>
  <Pause length="1"/>
  <Hangup/>
</Response>
"""
    return Response(xml, status=200, mimetype="text/xml")


# -----------------------------------------------------------------------------
# Lifecycle and process control
# -----------------------------------------------------------------------------

def _handle_sigterm(signum, frame):
    logging.info("Termination requested, shutting down dialer loop.")
    _stop_requested.set()


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def _start_background_threads() -> None:
    if not _dialer_thread.is_alive():
        _dialer_thread.start()


@atexit.register
def _shutdown():
    _stop_requested.set()
    if _dialer_thread.is_alive():
        _dialer_thread.join(timeout=2.0)


# -----------------------------------------------------------------------------
# CLI entrypoint
# -----------------------------------------------------------------------------
def main():
    # Informative log only; do not log environment values.
    logging.info("Scam Call Console starting.")
    # Start background dialer
    _start_background_threads()

    # Start Flask app
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    debug = _parse_bool(os.environ.get("FLASK_DEBUG"), False)
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
