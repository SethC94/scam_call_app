#!/usr/bin/env python3
"""
Scam Call Console: Outbound caller service with admin UI, call pacing, and Twilio integration.

This build adds:
- /api/status for frontend countdown and pacing indicators.
- /api/admin/env (GET/POST) for editing non-secret .env values from the Admin UI.
- Optional ngrok support: If USE_NGROK=true, a tunnel is started and PUBLIC_BASE_URL is set automatically.
- Larger countdown UI focus and number visibility improvements (via static assets).
- Clean shutdown on SIGINT/SIGTERM (Command+C).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# Optional dependencies
try:
    import bcrypt  # used when ADMIN_PASSWORD_HASH is configured
except Exception:
    bcrypt = None

try:
    from twilio.rest import Client  # type: ignore
except Exception:
    Client = None  # type: ignore

try:
    # Optional ngrok
    from pyngrok import ngrok as ngrok_lib  # type: ignore
except Exception:
    ngrok_lib = None  # type: ignore

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)  # type: ignore

# NOTE: configure a secure, stable secret in production via environment variable
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(32))

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# -----------------------------------------------------------------------------
# Helpers: parsing, time, dotenv
# -----------------------------------------------------------------------------

TRUE_SET = {"1", "true", "yes", "on", "y", "t"}

def _parse_bool(s: Optional[str], default: bool = False) -> bool:
    if s is None:
        return default
    return str(s).strip().lower() in TRUE_SET


def _parse_int(s: Optional[str], default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _parse_csv(s: Optional[str]) -> List[str]:
    if not s:
        return []
    return [p.strip() for p in str(s).split(",") if p.strip()]


def _now_local() -> datetime:
    # Present local time with timezone awareness if available
    try:
        return datetime.now().astimezone()
    except Exception:
        return datetime.now()


def _load_dotenv_pairs(path: str) -> List[Tuple[str, str]]:
    """
    Parse key=value pairs from a .env file without mutating os.environ.
    Keeps simple quoted values intact (single/double quotes).
    Ignores comments and blank lines.
    """
    pairs: List[Tuple[str, str]] = []
    p = Path(path)
    if not p.exists():
        return pairs
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1)
            val = m.group(2)
            if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                val = val[1:-1]
            pairs.append((key, val))
    except Exception as e:
        logging.error("Failed to read .env pairs: %s", e)
    return pairs


def _load_dotenv_lines(path: str) -> List[str]:
    """
    Return raw lines for .env, preserving order and comments.
    If file missing, returns empty list.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            return f.readlines()
    except Exception:
        return []


def _overlay_env_from_dotenv(path: str) -> None:
    """
    Load .env into process environment if a key is not already set.
    This is safe at startup to reflect a local .env without requiring python-dotenv.
    """
    for k, v in _load_dotenv_pairs(path):
        if k not in os.environ:
            os.environ[k] = v


# Load .env values at startup for convenience (does not overwrite existing process env)
_overlay_env_from_dotenv(".env")

# -----------------------------------------------------------------------------
# Runtime configuration
# -----------------------------------------------------------------------------

@dataclass
class RuntimeConfig:
    # Phone numbers
    to_number: str = ""
    from_number: str = ""
    from_numbers: List[str] = field(default_factory=list)

    # Pacing and schedule
    active_hours_local: str = "09:00-18:00"         # e.g., "09:00-18:00"
    active_days: List[str] = field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    min_interval_seconds: int = 120
    max_interval_seconds: int = 420
    hourly_max_attempts: int = 3
    daily_max_attempts: int = 20

    # Admin
    admin_user: Optional[str] = None
    admin_password_hash: Optional[str] = None  # bcrypt hash string if used

    # UI / misc
    rotate_prompts: bool = True
    rotate_prompts_strategy: str = "random"
    tts_voice: str = "man"
    tts_language: str = "en-US"
    recording_mode: str = "off"  # off|mono|dual|ask
    recording_jurisdiction_mode: str = "disable_in_two_party"

    # Networking
    public_base_url: Optional[str] = None
    use_ngrok: bool = False

    # Flask/server
    flask_host: str = "0.0.0.0"
    flask_port: int = 8080
    flask_debug: bool = False


_runtime = RuntimeConfig()

# Attempt tracking per destination number
_attempts_lock = threading.Lock()
_dest_attempts: Dict[str, List[float]] = {}  # to_number -> timestamps (epoch seconds)

# Next randomized interval bookkeeping (for UI status)
_next_call_epoch_s_lock = threading.Lock()
_next_call_epoch_s: Optional[int] = None
_interval_start_epoch_s: Optional[int] = None
_interval_total_seconds: Optional[int] = None

# Twilio client (lazy)
_twilio_client: Optional[Client] = None

# Ngrok
_active_tunnel_url: Optional[str] = None

# Lifecycle flags
_manual_call_requested = threading.Event()
_stop_requested = threading.Event()

# One-shot greeting (next call only)
_ONE_SHOT_GREETING: Optional[str] = None
_ONE_SHOT_GREETING_LOCK = threading.Lock()

# -----------------------------------------------------------------------------
# Config loading / refresh
# -----------------------------------------------------------------------------

def _normalize_day_name(s: str) -> Optional[str]:
    if not s:
        return None
    t = s.strip().lower()
    mapping = {
        "mon": "Mon", "monday": "Mon",
        "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
        "wed": "Wed", "weds": "Wed", "wednesday": "Wed",
        "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
        "fri": "Fri", "friday": "Fri",
        "sat": "Sat", "saturday": "Sat",
        "sun": "Sun", "sunday": "Sun",
    }
    return mapping.get(t)


def _load_runtime_from_env() -> None:
    _runtime.to_number = (os.environ.get("TO_NUMBER") or "").strip()
    _runtime.from_number = (os.environ.get("FROM_NUMBER") or "").strip()
    _runtime.from_numbers = _parse_csv(os.environ.get("FROM_NUMBERS"))

    _runtime.active_hours_local = (os.environ.get("ACTIVE_HOURS_LOCAL") or "09:00-18:00").strip()
    days = _parse_csv(os.environ.get("ACTIVE_DAYS") or "Mon,Tue,Wed,Thu,Fri")
    _runtime.active_days = [d for d in ([_normalize_day_name(x) for x in days]) if d]

    _runtime.min_interval_seconds = max(30, _parse_int(os.environ.get("MIN_INTERVAL_SECONDS"), 120))
    _runtime.max_interval_seconds = max(
        _runtime.min_interval_seconds,
        _parse_int(os.environ.get("MAX_INTERVAL_SECONDS"), 420),
    )
    _runtime.hourly_max_attempts = max(1, _parse_int(os.environ.get("HOURLY_MAX_ATTEMPTS_PER_DEST"), 3))
    _runtime.daily_max_attempts = max(_runtime.hourly_max_attempts, _parse_int(os.environ.get("DAILY_MAX_ATTEMPTS_PER_DEST"), 20))

    _runtime.rotate_prompts = _parse_bool(os.environ.get("ROTATE_PROMPTS"), True)
    _runtime.rotate_prompts_strategy = (os.environ.get("ROTATE_PROMPTS_STRATEGY") or "random").strip().lower()

    _runtime.tts_voice = (os.environ.get("TTS_VOICE") or "man").strip()
    _runtime.tts_language = (os.environ.get("TTS_LANGUAGE") or "en-US").strip()

    _runtime.recording_mode = (os.environ.get("RECORDING_MODE") or "off").strip().lower()
    _runtime.recording_jurisdiction_mode = (os.environ.get("RECORDING_JURISDICTION_MODE") or "disable_in_two_party").strip().lower()

    _runtime.admin_user = (os.environ.get("ADMIN_USER") or "").strip() or None
    _runtime.admin_password_hash = (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip() or None

    _runtime.public_base_url = (os.environ.get("PUBLIC_BASE_URL") or "").strip() or None
    _runtime.use_ngrok = _parse_bool(os.environ.get("USE_NGROK"), False)

    _runtime.flask_host = (os.environ.get("FLASK_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    _runtime.flask_port = _parse_int(os.environ.get("FLASK_PORT"), 8080)
    _runtime.flask_debug = _parse_bool(os.environ.get("FLASK_DEBUG"), False)

# Initial load
_load_runtime_from_env()

# -----------------------------------------------------------------------------
# Editable env: safe keys and persistence
# -----------------------------------------------------------------------------

_EDITABLE_ENV_KEYS = [
    # Phone numbers and pools
    "TO_NUMBER",
    "FROM_NUMBER",
    "FROM_NUMBERS",

    # Scheduling and pacing
    "ACTIVE_HOURS_LOCAL",
    "ACTIVE_DAYS",
    "MIN_INTERVAL_SECONDS",
    "MAX_INTERVAL_SECONDS",
    "HOURLY_MAX_ATTEMPTS_PER_DEST",
    "DAILY_MAX_ATTEMPTS_PER_DEST",

    # Recording and voice settings (non-secret toggles)
    "RECORDING_MODE",
    "RECORDING_JURISDICTION_MODE",
    "TTS_VOICE",
    "TTS_LANGUAGE",
    "ROTATE_PROMPTS",
    "ROTATE_PROMPTS_STRATEGY",

    # General non-secrets
    "COMPANY_NAME",
    "TOPIC",
    "ALLOWED_COUNTRY_CODES",
    "CALLEE_SILENCE_HANGUP_SECONDS",

    # Optional dev flags (non-secret)
    "USE_NGROK",
    "NONINTERACTIVE",
    "LOG_COLOR",
    "FLASK_HOST",
    "FLASK_PORT",
    "FLASK_DEBUG",
    "PUBLIC_BASE_URL",
]

_SECRET_ENV_KEYS = {
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "ADMIN_PASSWORD_HASH",
    "ADMIN_USER",  # not a secret per se, but do not expose/edit via UI here
    "FLASK_SECRET",
}

def _current_env_editable_pairs() -> List[Tuple[str, str]]:
    """
    Combine process environment and .env to represent current editable values.
    Order follows _EDITABLE_ENV_KEYS.
    """
    effective: Dict[str, str] = {}
    for k in _EDITABLE_ENV_KEYS:
        effective[k] = (os.environ.get(k) or "").strip()

    # Overlay .env file values if present to reflect file state
    try:
        env_path = Path(".env")
        if env_path.exists():
            for k, v in _load_dotenv_pairs(str(env_path)):
                if k in _EDITABLE_ENV_KEYS:
                    effective[k] = (v or "").strip()
    except Exception:
        pass

    return [(k, effective.get(k, "")) for k in _EDITABLE_ENV_KEYS]


def _write_env_updates_preserving_comments(updates: Dict[str, str]) -> None:
    """
    Persist updates for editable keys into .env, preserving comments and unrelated lines.
    """
    env_path = Path(".env")
    lines = _load_dotenv_lines(str(env_path))
    key_to_idx: Dict[str, int] = {}

    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        eq = s.find("=")
        if eq <= 0:
            continue
        k = s[:eq].strip()
        if k in _EDITABLE_ENV_KEYS:
            key_to_idx[k] = idx

    content = list(lines)
    for k, v in updates.items():
        if k not in _EDITABLE_ENV_KEYS:
            continue
        safe_v = "" if v is None else str(v)
        new_line = f"{k}={safe_v}\n"
        if k in key_to_idx:
            content[key_to_idx[k]] = new_line
        else:
            if content and not content[-1].endswith("\n"):
                content[-1] = content[-1] + "\n"
            content.append(new_line)

    tmp = env_path.with_suffix(".tmp")
    bak = env_path.with_suffix(".bak")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        if env_path.exists():
            if bak.exists():
                try:
                    bak.unlink()
                except Exception:
                    pass
            env_path.replace(bak)
    except Exception:
        pass
    os.replace(tmp, env_path)


def _apply_env_updates(updates: Dict[str, str]) -> None:
    """
    Apply updates: write to .env, update process env for current process, and refresh runtime config.
    """
    # Persist first
    _write_env_updates_preserving_comments(updates)

    # Update current process environment
    for k, v in updates.items():
        if k in _EDITABLE_ENV_KEYS and k not in _SECRET_ENV_KEYS:
            os.environ[k] = "" if v is None else str(v)

    # Refresh runtime view
    _load_runtime_from_env()


# -----------------------------------------------------------------------------
# Attempt tracking and active window logic
# -----------------------------------------------------------------------------

def _within_active_window(now_local: datetime) -> bool:
    """
    Determine if current local time is within configured active window (hours and days).
    active_hours_local: "HH:MM-HH:MM" in local time.
    active_days: List of day names ["Mon", "Tue", ...].
    """
    try:
        start_str, end_str = (_runtime.active_hours_local or "09:00-18:00").split("-", 1)
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
    except Exception:
        sh, sm, eh, em = 9, 0, 18, 0

    wd_map = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today = wd_map[now_local.weekday()]
    if _runtime.active_days and today not in _runtime.active_days:
        return False

    t_minutes = now_local.hour * 60 + now_local.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m <= end_m:
        return start_m <= t_minutes <= end_m
    # Window wraps midnight
    return t_minutes >= start_m or t_minutes <= end_m


def _prune_attempts(now_ts: int, to_number: str) -> None:
    with _attempts_lock:
        lst = _dest_attempts.get(to_number, [])
        # Keep 24h window
        cutoff = now_ts - 24 * 3600
        _dest_attempts[to_number] = [t for t in lst if t >= cutoff]


def _note_attempt(now_ts: float, to_number: str) -> None:
    with _attempts_lock:
        _dest_attempts.setdefault(to_number, []).append(now_ts)


def _can_attempt(now_ts: int, to_number: str) -> Tuple[bool, int]:
    """
    Return (allowed, wait_seconds_if_capped).
    Enforces hourly and daily limits for the destination.
    """
    _prune_attempts(now_ts, to_number)
    with _attempts_lock:
        lst = _dest_attempts.get(to_number, [])
        last_hour = [t for t in lst if t >= now_ts - 3600]
        if len(last_hour) >= _runtime.hourly_max_attempts:
            # Approximate wait until the oldest of last hour window expires
            oldest = min(last_hour) if last_hour else now_ts
            return False, max(1, (oldest + 3600) - now_ts)
        if len(lst) >= _runtime.daily_max_attempts:
            # Wait until next day (simplified)
            return False, 3600  # 1 hour conservative back-off
    return True, 0


# -----------------------------------------------------------------------------
# Twilio handling (minimal; integrate as needed for outbound placement)
# -----------------------------------------------------------------------------

def _ensure_twilio_client() -> Optional[Client]:
    global _twilio_client
    if _twilio_client is not None:
        return _twilio_client
    if Client is None:
        logging.error("Twilio SDK not available. Install with: pip install twilio")
        return None
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not tok:
        logging.error("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in environment.")
        return None
    _twilio_client = Client(sid, tok)
    return _twilio_client


def _choose_from_number() -> Optional[str]:
    if _runtime.from_numbers:
        return random.choice(_runtime.from_numbers)
    return _runtime.from_number or None


# -----------------------------------------------------------------------------
# Background dialer (simplified loop honoring pacing and stop events)
# -----------------------------------------------------------------------------

def _compute_next_interval_seconds() -> int:
    lo = max(30, int(_runtime.min_interval_seconds))
    hi = max(lo, int(_runtime.max_interval_seconds))
    if lo == hi:
        return lo
    return random.randint(lo, hi)


def _dialer_loop() -> None:
    global _next_call_epoch_s, _interval_start_epoch_s, _interval_total_seconds
    logging.info("Dialer thread started.")
    while not _stop_requested.is_set():
        now = int(time.time())

        # Determine next planned attempt time (interval-based)
        with _next_call_epoch_s_lock:
            if _next_call_epoch_s is None:
                # Initialize an interval starting now
                _interval_total_seconds = _compute_next_interval_seconds()
                _interval_start_epoch_s = now
                _next_call_epoch_s = now + int(_interval_total_seconds or 0)

        # Manual trigger: attempt immediately if allowed
        if _manual_call_requested.is_set():
            _manual_call_requested.clear()

            if not _runtime.to_number:
                logging.info("TO_NUMBER not configured; skipping manual attempt.")
            else:
                can, wait_s = _can_attempt(now, _runtime.to_number)
                if not can:
                    logging.info("Attempt capped; wait %ss.", wait_s)
                elif not _within_active_window(_now_local()):
                    logging.info("Outside active window; attempt suppressed.")
                else:
                    # Place call via Twilio (minimal; extend as needed)
                    client = _ensure_twilio_client()
                    from_n = _choose_from_number()
                    if client and from_n:
                        try:
                            # In a full build, provide proper TwiML URLs
                            public_url = _runtime.public_base_url or ""
                            # Fallback simple say TwiML if no PUBLIC_BASE_URL
                            if not public_url:
                                # This is a basic TwiML bin served by Twilio if configured; otherwise, skip placement
                                logging.info("PUBLIC_BASE_URL not configured; call not placed to avoid Twilio callback failures.")
                            else:
                                call = client.calls.create(
                                    to=_runtime.to_number,
                                    from_=from_n,
                                    url=f"{public_url}/voice",
                                    status_callback=f"{public_url}/status",
                                    status_callback_event=["initiated", "ringing", "answered", "completed"],
                                    status_callback_method="POST",
                                )
                                logging.info("Placed call: %s", getattr(call, "sid", "<sid>"))
                                _note_attempt(time.time(), _runtime.to_number)
                        except Exception as e:
                            logging.error("Twilio call placement failed: %s", e)
                    else:
                        logging.info("Twilio not configured or no FROM number; cannot place call.")

        # Wait a short step with responsiveness to stop signal
        for _ in range(5):
            if _stop_requested.is_set():
                break
            time.sleep(0.2)

        # Maintain rolling interval timing for UI
        now = int(time.time())
        with _next_call_epoch_s_lock:
            if _next_call_epoch_s is not None and now >= _next_call_epoch_s:
                # Start a new interval
                _interval_total_seconds = _compute_next_interval_seconds()
                _interval_start_epoch_s = now
                _next_call_epoch_s = now + int(_interval_total_seconds or 0)

    logging.info("Dialer thread stopped.")


_dialer_thread = threading.Thread(target=_dialer_loop, name="dialer-thread", daemon=True)

# -----------------------------------------------------------------------------
# Admin authentication helpers
# -----------------------------------------------------------------------------

def _admin_defaults() -> Tuple[str, Optional[str], bool]:
    """
    Returns (username, bcrypt_hash_or_None, uses_hash_bool)
    """
    env_user = (_runtime.admin_user or "").strip() if _runtime.admin_user else None
    env_hash = (_runtime.admin_password_hash or "").strip() if _runtime.admin_password_hash else None
    if env_user and env_hash and bcrypt is not None:
        return env_user, env_hash, True
    # Default development credentials (do not use in production)
    return "bootycall", None, False


def _admin_authenticated() -> bool:
    return bool(session.get("is_admin") is True)


def _require_admin_for_api() -> Optional[Response]:
    if not _admin_authenticated():
        return Response(json.dumps({"error": "unauthorized"}), status=401, mimetype="application/json")
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
        ok = (username == effective_user and password == "scammers")

    if not ok:
        return render_template("admin_login.html", error="Invalid credentials.")

    session["is_admin"] = True
    return redirect(url_for("scamcalls"))


@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("scamcalls"))


@app.route("/api/admin/env", methods=["GET"])
def api_admin_env_get():
    resp = _require_admin_for_api()
    if resp:
        return resp
    editable = [{"key": k, "value": v} for (k, v) in _current_env_editable_pairs()]
    return jsonify({"editable": editable})


@app.route("/api/admin/env", methods=["POST"])
def api_admin_env_post():
    resp = _require_admin_for_api()
    if resp:
        return resp
    try:
        data = request.get_json(force=True, silent=False) or {}
        updates_raw = data.get("updates") or {}
        if not isinstance(updates_raw, dict):
            return Response("Invalid payload.", status=400)
        # Normalize and filter
        clean_updates: Dict[str, str] = {}
        for k, v in updates_raw.items():
            if k in _SECRET_ENV_KEYS:
                continue
            if k in _EDITABLE_ENV_KEYS:
                clean_updates[str(k)] = "" if v is None else str(v)
        _apply_env_updates(clean_updates)
        return jsonify({"ok": True})
    except Exception as e:
        logging.error("Failed to save env updates: %s", e)
        return Response("Failed to save settings.", status=500)


@app.route("/api/call-now", methods=["POST"])
def api_call_now():
    # Enforce active window and caps at request time
    if not _within_active_window(_now_local()):
        return jsonify(ok=False, reason="outside_active_window", message="Outside active calling window."), 200

    if not _runtime.to_number:
        return jsonify(ok=False, reason="missing_destination", message="TO_NUMBER is not configured."), 400

    now = int(time.time())
    allowed, wait_s = _can_attempt(now, _runtime.to_number)
    if not allowed:
        return jsonify(ok=False, reason="cap_reached", wait_seconds=wait_s), 429

    _manual_call_requested.set()
    return jsonify(ok=True)


@app.route("/api/next-greeting", methods=["POST"])
def api_next_greeting():
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    phrase = (data.get("phrase") or "").strip()

    words = [w for w in re.split(r"\s+", phrase) if w]
    if not (5 <= len(words) <= 15):
        return Response("Phrase must be between 5 and 15 words.", status=400)

    global _ONE_SHOT_GREETING
    with _ONE_SHOT_GREETING_LOCK:
        _ONE_SHOT_GREETING = phrase
    return jsonify(ok=True)


@app.route("/api/status", methods=["GET"])
def api_status():
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
        # Expose numbers for UI display
        "to_number": _runtime.to_number,
        "from_number": _runtime.from_number,
        "from_numbers": _runtime.from_numbers,
    }
    return jsonify(payload)

# -----------------------------------------------------------------------------
# Ngrok management (optional)
# -----------------------------------------------------------------------------

def _start_ngrok_if_enabled() -> None:
    global _active_tunnel_url
    if not _runtime.use_ngrok:
        return
    if ngrok_lib is None:
        logging.warning("USE_NGROK=true but pyngrok is not installed. Skipping.")
        return
    try:
        if _active_tunnel_url:
            return
        port = _runtime.flask_port or 8080
        tun = ngrok_lib.connect(addr=port, proto="http")
        _active_tunnel_url = tun.public_url  # type: ignore
        os.environ["PUBLIC_BASE_URL"] = _active_tunnel_url
        _runtime.public_base_url = _active_tunnel_url
        logging.info("ngrok tunnel active at %s", _active_tunnel_url)
    except Exception as e:
        logging.error("Failed to start ngrok: %s", e)


@atexit.register
def _shutdown_ngrok():
    try:
        if ngrok_lib is not None:
            ngrok_lib.kill()
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Lifecycle and process control
# -----------------------------------------------------------------------------

def _handle_termination(signum, frame):
    logging.info("Termination signal received (%s). Stopping service.", signum)
    try:
        _stop_requested.set()
    except Exception:
        pass

signal.signal(signal.SIGTERM, _handle_termination)
signal.signal(signal.SIGINT, _handle_termination)

def _start_background_threads() -> None:
    if not _dialer_thread.is_alive():
        _dialer_thread.start()

# -----------------------------------------------------------------------------
# CLI entrypoint
# -----------------------------------------------------------------------------

def main():
    logging.info("Scam Call Console starting.")

    # Start ngrok (optional)
    _start_ngrok_if_enabled()

    # Start background dialer
    _start_background_threads()

    host = _runtime.flask_host or os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(_runtime.flask_port or _parse_int(os.environ.get("FLASK_PORT"), 8080))
    debug = bool(_runtime.flask_debug or _parse_bool(os.environ.get("FLASK_DEBUG"), False))

    # Important: disable reloader so SIGINT/SIGTERM reach this process directly
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
