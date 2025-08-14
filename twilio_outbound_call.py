#!/usr/bin/env python3
"""
Twilio outbound caller with web UI (/scamcalls) and live audio monitor.

Key capabilities
- Outbound call attempts on a randomized interval between MIN_INTERVAL_SECONDS and MAX_INTERVAL_SECONDS.
- UI at /scamcalls shows a live transcript when connected and a countdown ring between calls.
- Optional "Call now" to immediately request an attempt (respects active window, caps, cooldowns).
- Live audio monitor on /scamcalls via Twilio Media Streams relayed to the browser through WebSockets.
  Note: Twilio Media Streams do not add a separate feature charge beyond normal voice minutes.
- Status handling and transcript/history collection with CSV persistence across restarts.
- Non-blocking startup for background runs: if no input within 10 seconds (or no TTY), proceed with defaults.
- Rotating opening messages per call (sequential or random).
- Always use a random FROM number when FROM_NUMBERS is provided.

Requirements
- Python 3.8+
- pip install twilio flask pyngrok python-dotenv flask-sock simple-websocket
"""

import os
import re
import sys
import csv
import json
import time
import random
import signal
import logging
import threading
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, Set, Dict, Any, List

# Optional .env auto-load
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Ensure pyngrok uses local binary if set (optional)
os.environ.setdefault("NGROK_PATH", "/opt/homebrew/bin/ngrok")

from flask import Flask, request, Response, render_template, jsonify, send_from_directory, session

# WebSockets
try:
    from flask_sock import Sock
except Exception:
    print("Missing dependency: pip install flask-sock simple-websocket", file=sys.stderr)
    raise

# Requests is used by the Twilio SDK
try:
    import requests
except Exception:
    requests = None

try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    from twilio.http.http_client import TwilioHttpClient
except Exception:
    print("Missing dependency: pip install twilio", file=sys.stderr)
    raise

# Optional ngrok
try:
    from pyngrok import ngrok, conf as ngrok_conf
    from pyngrok.conf import PyngrokConfig
    _HAS_NGROK = True
except Exception:
    _HAS_NGROK = False

# Prompts file
try:
    from rotating_iv_prompts import PROMPTS as ROTATING_PROMPTS
except Exception:
    ROTATING_PROMPTS = []

# Flask app and WebSocket sock
app = Flask(__name__)
sock = Sock(app)

# Session configuration for admin login
app.secret_key = os.getenv("ADMIN_SESSION_SECRET", secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Admin configuration
_ADMIN_USERNAME = "bootycall"
_ADMIN_PASSWORD = "scammers"
_admin_failed_attempts: Dict[str, List[float]] = {}
_admin_lockout_until: Dict[str, float] = {}

# Environment variables that are safe to edit (exclude secrets)
_SECRET_PATTERNS = {
    "token", "secret", "password", "pass", "auth", "key", "sid", 
    "authtoken", "account_sid", "auth_token"
}

# Manual greeting phrase storage (consumed on next call)
_manual_greeting_phrase: Optional[str] = None
_greeting_lock = threading.Lock()

# Reload status tracking
_reload_requested = False
_reload_status_lock = threading.Lock()

# Globals
_STOP_REQUESTED = False
ANSI_BOLD = ""
ANSI_CYAN = ""
ANSI_RESET = ""

# Runtime configuration
_TTS_VOICE: str = os.getenv("TTS_VOICE", "man").strip() or "man"
_TTS_LANGUAGE: str = os.getenv("TTS_LANGUAGE", "en-US").strip() or "en-US"
_COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Your Company").strip() or "Your Company"
_TOPIC: str = os.getenv("TOPIC", "engine replacement").strip() or "engine replacement"

_GREETING_WAIT_TIMEOUT_S: int = max(1, min(10, int(os.getenv("GREETING_WAIT_TIMEOUT_SECONDS", "3"))))
_GREETING_MAX_CYCLES: int = max(1, int(os.getenv("GREETING_MAX_CYCLES", "3")))
_DEFAULT_GREETING_KEYWORDS = [
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
    "this is", "speaking", "yes", "how are you", "how can i help", "go ahead"
]
_GREETING_KEYWORDS: List[str] = [kw.strip().lower() for kw in os.getenv(
    "GREETING_KEYWORDS",
    ",".join(_DEFAULT_GREETING_KEYWORDS)
).split(",") if kw.strip()]

_ENABLE_AMD: bool = os.getenv("ENABLE_AMD", "false").strip().lower() in {"1", "true", "yes", "on"}
_AMD_MODE: str = os.getenv("AMD_MODE", "Enable").strip() or "Enable"
try:
    _AMD_TIMEOUT_S: int = max(3, min(59, int(os.getenv("AMD_TIMEOUT_SECONDS", "20"))))
except ValueError:
    _AMD_TIMEOUT_S = 20

# Recording configuration
_ENABLE_RECORDING_ENV: bool = os.getenv("ENABLE_RECORDING", "false").strip().lower() in {"1", "true", "yes", "on"}
_RECORD_CALLS: bool = _ENABLE_RECORDING_ENV or (os.getenv("RECORD_CALLS", "false").strip().lower() in {"1", "true", "yes", "on"})
_RECORDING_CHANNELS: str = os.getenv("RECORDING_CHANNELS", "mono").strip().lower() or "mono"
_RECORDING_STATUS_EVENTS: List[str] = [e.strip() for e in os.getenv("RECORDING_STATUS_EVENTS", "in-progress,completed").split(",") if e.strip()]

# Pacing
try:
    _MIN_INTERVAL_S = max(30, int(os.getenv("MIN_INTERVAL_SECONDS", "120")))
except ValueError:
    _MIN_INTERVAL_S = 120
try:
    _MAX_INTERVAL_S = max(_MIN_INTERVAL_S, int(os.getenv("MAX_INTERVAL_SECONDS", "420")))
except ValueError:
    _MAX_INTERVAL_S = max(_MIN_INTERVAL_S, 420)

try:
    _DAILY_MAX_PER_DEST = max(1, int(os.getenv("DAILY_MAX_ATTEMPTS_PER_DEST", "12")))
except ValueError:
    _DAILY_MAX_PER_DEST = 12
try:
    _HOURLY_MAX_PER_DEST = max(1, int(os.getenv("HOURLY_MAX_ATTEMPTS_PER_DEST", "3")))
except ValueError:
    _HOURLY_MAX_PER_DEST = 3

_ACTIVE_HOURS_LOCAL = os.getenv("ACTIVE_HOURS_LOCAL", "09:00-18:00").strip()
_ACTIVE_DAYS = [d.strip().title() for d in os.getenv("ACTIVE_DAYS", "Mon,Tue,Wed,Thu,Fri").split(",") if d.strip()]
_BACKOFF_STRATEGY = os.getenv("BACKOFF_STRATEGY", "none").strip().lower()

# Caps
try:
    _MAX_CALL_DURATION_S = max(10, int(os.getenv("MAX_CALL_DURATION_SECONDS", "60")))
except ValueError:
    _MAX_CALL_DURATION_S = 60

# Silence hangup
try:
    _CALLEE_SILENCE_HANGUP_S = max(5, min(60, int(os.getenv("CALLEE_SILENCE_HANGUP_SECONDS", "10"))))
except ValueError:
    _CALLEE_SILENCE_HANGUP_S = 10

# Non-interactive, mirroring
_NONINTERACTIVE = os.getenv("NONINTERACTIVE", "false").strip().lower() in {"1", "true", "yes", "on"}
_MIRROR_DIR = os.getenv("MIRROR_TRANSCRIPTS_DIR", "").strip() or ""

# History persistence (CSV)
_HISTORY_CSV_PATH = os.getenv("HISTORY_CSV_PATH", os.path.join(".", "data", "call_history.csv"))
_history_file_lock = threading.Lock()
_persisted_call_sids: Set[str] = set()

# Twilio client (initialized in main)
_TWILIO_CLIENT: Optional[Client] = None

# In-memory call state
_call_state_lock = threading.Lock()
# CallSid -> {...}
_call_state: Dict[str, Dict[str, Any]] = {}

# Diagnostics
_diag_lock = threading.Lock()
_call_diag: Dict[str, Dict[str, Any]] = {}

# Attempt tracking
_attempts_lock = threading.Lock()
_dest_attempts: Dict[str, List[float]] = {}
_dest_backoff: Dict[str, Dict[str, Any]] = {}

# FROM numbers
_from_lock = threading.Lock()
_FROM_NUMBERS: List[str] = []
try:
    _FROM_COOLDOWN_MIN = max(0, int(os.getenv("FROM_NUMBER_MIN_COOLDOWN_MIN", "30")))
except ValueError:
    _FROM_COOLDOWN_MIN = 30
# When a pool is provided, this build uses a purely random pick per attempt.

# Web UI support state
_PUBLIC_BASE_URL: Optional[str] = None
_DEST_NUMBER: Optional[str] = None
_last_from_number: Optional[str] = None
_next_call_epoch_s: Optional[int] = None
_next_call_start_epoch_s: Optional[int] = None

_history_lock = threading.Lock()
# List of dicts: {callSid, startedAt, durationSec, outcome, transcript:[{role, text, ts}], prompt: str}
_history: List[Dict[str, Any]] = []

# Manual "Call now" signal
_manual_call_requested = threading.Event()

# Prompt rotation settings
_ROTATE_PROMPTS = os.getenv("ROTATE_PROMPTS", "true").strip().lower() in {"1", "true", "yes", "on"}
_ROTATE_PROMPTS_STRATEGY = os.getenv("ROTATE_PROMPTS_STRATEGY", "sequential").strip().lower()  # "sequential" or "random"
_prompts_list: List[str] = [str(p).strip() for p in ROTATING_PROMPTS if str(p).strip()]
_prompt_lock = threading.Lock()
_prompt_index = 0
_last_random_index: Optional[int] = None

# Live audio: connected browser clients
_audio_clients_lock = threading.Lock()
_audio_clients: Set[Any] = set()  # set of WebSocket objects


def setup_logging() -> None:
    global ANSI_BOLD, ANSI_CYAN, ANSI_RESET
    color_enabled = os.getenv("LOG_COLOR", "1").strip().lower() not in {"0", "false", "no", "off"}
    ANSI_BOLD = "\033[1m" if color_enabled else ""
    ANSI_CYAN = "\033[36m" if color_enabled else ""
    ANSI_RESET = "\033[0m" if color_enabled else ""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%-Y-%m-%d %H:%M:%S" if sys.platform != "win32" else "%Y-%m-%d %H:%M:%S",
    )


def _handle_stop(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    logging.info("Stop requested; exiting after current cycle.")


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _recording_enabled() -> bool:
    return _RECORD_CALLS


def _ensure_diag_state(call_sid: str) -> Dict[str, Any]:
    with _diag_lock:
        d = _call_diag.get(call_sid)
        if d is None:
            d = {}
            _call_diag[call_sid] = d
        d.setdefault("created_ts", time.time())
        d.setdefault("events", [])
        d.setdefault("ringing_ts", None)
        d.setdefault("answered_ts", None)
        d.setdefault("answered_by", None)
        d.setdefault("final_status", None)
        d.setdefault("sip_code", None)
        d.setdefault("duration", None)
        d.setdefault("recording_urls", [])
        d.setdefault("recording_sids", [])
        d.setdefault("from_number", None)
        d.setdefault("interval_chosen_s", None)
        d.setdefault("backoff_applied_s", 0)
        d.setdefault("prompt", None)
        return d


def _ensure_call_state(call_sid: str) -> None:
    with _call_state_lock:
        if call_sid not in _call_state:
            log_path = None
            if _MIRROR_DIR:
                try:
                    os.makedirs(_MIRROR_DIR, exist_ok=True)
                    log_path = os.path.join(_MIRROR_DIR, f"call_{call_sid}.log")
                except Exception:
                    log_path = None
            _call_state[call_sid] = {
                "start_ts": time.time(),
                "timer": None,
                "segments": [],
                "closed": False,
                "fsm_state": "WAIT_HELLO",
                "live_utterance": {"buffer": "", "last_partial_ts": 0.0, "inactivity_timer": None},
                "context": {
                    "year": None, "make": None, "model": None,
                    "engine": None, "vin8": None, "location": None,
                    "budget": None, "timeline": None, "agreed": None
                },
                "seq": 1,
                "log_path": log_path,
            }
    _ensure_diag_state(call_sid)


def _write_mirror(call_sid: str, line: str) -> None:
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        path = st.get("log_path")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _append_line(call_sid: str, role: str, text: str) -> None:
    if not text:
        return
    labeled = f"{role}: {text}"
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        st["segments"].append(labeled)
    header = f"{ANSI_BOLD}{ANSI_CYAN}=== {role} line (CallSid={call_sid}) ==={ANSI_RESET}"
    footer = f"{ANSI_BOLD}{ANSI_CYAN}=== End {role} line ==={ANSI_RESET}"
    logging.info("\n%s\n%s\n%s", header, labeled, footer)
    _write_mirror(call_sid, labeled)


def _append_partial_callee(call_sid: str, text: str) -> None:
    if not text:
        return
    labeled = f"Callee (partial): {text}..."
    header = f"{ANSI_BOLD}{ANSI_CYAN}=== Callee partial (CallSid={call_sid}) ==={ANSI_RESET}"
    footer = f"{ANSI_BOLD}{ANSI_CYAN}=== End partial ==={ANSI_RESET}"
    logging.info("\n%s\n%s\n%s", header, labeled, footer)
    _write_mirror(call_sid, labeled)


def _append_callee_line(call_sid: str, text: str) -> None:
    _append_line(call_sid, "Callee", text)


def _append_assistant_line(call_sid: str, text: str) -> None:
    _append_line(call_sid, "Assistant", text)


def _finalize_transcript(call_sid: str) -> str:
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return ""
        full_text = "\n".join(st["segments"]).strip()
        st["closed"] = True
    header = f"{ANSI_BOLD}{ANSI_CYAN}===== Full conversation (CallSid={call_sid}) ====={ANSI_RESET}"
    footer = f"{ANSI_BOLD}{ANSI_CYAN}===== End conversation ====={ANSI_RESET}"
    if full_text:
        logging.info("\n%s\n%s\n%s", header, full_text, footer)
    else:
        logging.info("\n%s\n%s\n%s", header, "<no speech captured>", footer)
    return full_text


def _schedule_forced_hangup(call_sid: str, seconds: int) -> None:
    def _hangup():
        try:
            client = _TWILIO_CLIENT
            if client is None:
                return
            client.calls(call_sid).update(status="completed")
            logging.info("Forced hangup triggered at %ss for CallSid=%s", seconds, call_sid)
        except Exception as e:
            logging.error("Failed to force hangup for CallSid=%s: %s", call_sid, str(e))

    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        if st.get("timer"):
            try:
                st["timer"].cancel()
            except Exception:
                pass
        t = threading.Timer(seconds, _hangup)
        st["timer"] = t
        t.daemon = True
        t.start()


def _cancel_forced_hangup(call_sid: str) -> None:
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        t = st.get("timer")
        if t:
            try:
                t.cancel()
            except Exception:
                pass
        st["timer"] = None


def _record_event(call_sid: str, event: str, call_status: str, answered_by: Optional[str], sip_code: Optional[str], duration: Optional[str]) -> None:
    now = time.time()
    d = _ensure_diag_state(call_sid)
    with _diag_lock:
        d["events"].append({
            "t": now,
            "event": event,
            "call_status": call_status,
            "answered_by": answered_by,
            "sip_code": sip_code,
            "duration": duration,
        })
        if event == "ringing" and d.get("ringing_ts") is None:
            d["ringing_ts"] = now
        if event == "answered":
            d["answered_ts"] = now
            if answered_by:
                d["answered_by"] = answered_by
        if event == "completed":
            d["final_status"] = call_status
            if sip_code:
                d["sip_code"] = sip_code
            if duration is not None:
                d["duration"] = duration


def _classify_outcome(call_sid: str) -> str:
    with _diag_lock:
        d = _call_diag.get(call_sid, {})

    ans_by = (d.get("answered_by") or "").lower()
    ring_ts = d.get("ringing_ts")
    ans_ts = d.get("answered_ts")
    final_status = (d.get("final_status") or "").lower()
    sip = d.get("sip_code")
    try:
        duration_s = int(d.get("duration")) if d.get("duration") is not None else None
    except Exception:
        duration_s = None

    if ans_by.startswith("human"):
        return "Human answered"
    if ans_by.startswith("machine"):
        if ans_ts is not None:
            if ring_ts is None or (ans_ts - d.get("created_ts", ans_ts)) < 3:
                return "Voicemail or immediate forward"
            if ring_ts is not None and (ans_ts - ring_ts) >= 10:
                return "No answer; voicemail after ringing"
        return "Voicemail detected"
    if final_status == "busy" or sip in {"486"}:
        return "Busy"
    if final_status == "failed" or sip in {"603", "607", "403"}:
        return "Declined/Blocked"
    if final_status in {"no-answer", "canceled"}:
        return "No answer"
    if final_status == "completed" and (duration_s == 0):
        return "Completed with zero duration"
    return "Outcome unknown"


def twiml_response(xml: str) -> Response:
    return Response(xml, status=200, mimetype="text/xml")


def _utterance_inactivity_commit(call_sid: str) -> None:
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        live = st["live_utterance"]
        text = live.get("buffer", "").strip()
        live["buffer"] = ""
        live["inactivity_timer"] = None
    if text:
        _append_callee_line(call_sid, text)


def _buffer_partial(call_sid: str, partial: str, inactivity_ms: int = 1000) -> None:
    now = time.time()
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        live = st["live_utterance"]
        live["buffer"] = partial
        live["last_partial_ts"] = now
        tmr: Optional[threading.Timer] = live.get("inactivity_timer")
        if tmr:
            try:
                tmr.cancel()
            except Exception:
                pass
        t = threading.Timer(inactivity_ms / 1000.0, _utterance_inactivity_commit, args=(call_sid,))
        t.daemon = True
        live["inactivity_timer"] = t
        t.start()


def _clear_live_utterance(call_sid: str) -> None:
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return
        live = st["live_utterance"]
        live["buffer"] = ""
        tmr = live.get("inactivity_timer")
        if tmr:
            try:
                tmr.cancel()
            except Exception:
                pass
        live["inactivity_timer"] = None


def _parse_active_window(active_str: str) -> Tuple[int, int]:
    try:
        s, e = active_str.split("-")
        hs, ms = [int(x) for x in s.strip().split(":")]
        he, me = [int(x) for x in e.strip().split(":")]
        return hs * 60 + ms, he * 60 + me
    except Exception:
        return 9 * 60, 18 * 60


def _now_local() -> datetime:
    return datetime.now()


def _within_active_window(now_dt: datetime) -> bool:
    day = now_dt.strftime("%a")
    if day not in _ACTIVE_DAYS:
        return False
    start_min, end_min = _parse_active_window(_ACTIVE_HOURS_LOCAL)
    mins = now_dt.hour * 60 + now_dt.minute
    if start_min <= end_min:
        return start_min <= mins < end_min
    return mins >= start_min or mins < end_min


def _time_until_active_window(now_dt: datetime) -> int:
    if _within_active_window(now_dt):
        return 0
    start_min, _ = _parse_active_window(_ACTIVE_HOURS_LOCAL)
    for add_days in range(0, 8):
        candidate = now_dt + timedelta(days=add_days)
        if candidate.strftime("%a") in _ACTIVE_DAYS:
            target = candidate.replace(hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0)
            if target > now_dt:
                return int((target - now_dt).total_seconds())
    target = (now_dt + timedelta(days=1)).replace(hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0)
    return int((target - now_dt).total_seconds())


def _prune_attempts(now_ts: float, to_number: str) -> None:
    with _attempts_lock:
        lst = _dest_attempts.get(to_number, [])
        one_day_ago = now_ts - 86400
        _dest_attempts[to_number] = [t for t in lst if t >= one_day_ago]


def _can_attempt(now_ts: float, to_number: str) -> Tuple[bool, int]:
    _prune_attempts(now_ts, to_number)
    with _attempts_lock:
        lst = _dest_attempts.get(to_number, [])
        last_hour = [t for t in lst if t >= now_ts - 3600]
        last_day = lst
        hourly_ok = len(last_hour) < _HOURLY_MAX_PER_DEST
        daily_ok = len(last_day) < _DAILY_MAX_PER_DEST
        wait_hour = 0
        wait_day = 0
        if not hourly_ok and last_hour:
            next_allowed_hour = min(last_hour) + 3600
            wait_hour = max(0, int(next_allowed_hour - now_ts))
        if not daily_ok and last_day:
            next_allowed_day = min(last_day) + 86400
            wait_day = max(0, int(next_allowed_day - now_ts))
    with _attempts_lock:
        bo = _dest_backoff.get(to_number)
        wait_bo = 0
        if bo and bo.get("next_earliest_ts"):
            wait_bo = max(0, int(bo["next_earliest_ts"] - now_ts))
    wait_total = max(wait_hour, wait_day, wait_bo)
    return hourly_ok and daily_ok and wait_bo == 0, wait_total


def _update_backoff(to_number: str, outcome: str) -> None:
    immediate_fail = outcome in {
        "Busy",
        "Declined/Blocked",
        "No answer",
        "Voicemail detected",
        "No answer; voicemail after ringing",
        "Voicemail or immediate forward",
        "Completed with zero duration",
    }
    with _attempts_lock:
        state = _dest_backoff.setdefault(to_number, {"fail_count": 0, "next_earliest_ts": 0.0})
        if _BACKOFF_STRATEGY == "none":
            state["fail_count"] = 0
            state["next_earliest_ts"] = 0.0
            return
        if immediate_fail:
            state["fail_count"] = int(state.get("fail_count", 0)) + 1
        else:
            state["fail_count"] = 0
            state["next_earliest_ts"] = 0.0
            return
        base_delay = _MIN_INTERVAL_S
        delay = base_delay * state["fail_count"] if _BACKOFF_STRATEGY == "linear" else base_delay * (2 ** (state["fail_count"] - 1))
        max_delay = 3600
        delay = min(delay, max_delay)
        state["next_earliest_ts"] = time.time() + delay


def normalize_to_e164(number: str) -> str:
    if not number:
        raise ValueError("Empty phone number.")
    n = re.sub(r"[^\d+]", "", number.strip())
    if n.startswith("+"):
        if re.fullmatch(r"\+\d{8,15}", n):
            return n
        raise ValueError(f"Invalid E.164 format: {number}")
    if re.fullmatch(r"1\d{10}", n):
        return f"+{n}"
    if re.fullmatch(r"\d{10}", n):
        return f"+1{n}"
    raise ValueError(f"Cannot normalize number to E.164: {number}")


def parse_allowed_country_codes(env_val: Optional[str]) -> Set[str]:
    default = {"+1"}
    if not env_val:
        return default
    codes: Set[str] = set()
    for part in env_val.split(","):
        part = part.strip()
        if not part:
            continue
        if not re.fullmatch(r"\+\d{1,3}", part):
            raise ValueError(f"Invalid country code: {part}")
        codes.add(part)
    return codes or default


def enforce_country_allowlist(e164_number: str, allowed: Set[str]) -> None:
    if not re.fullmatch(r"\+\d{8,15}", e164_number):
        raise ValueError(f"Not a valid E.164 number: {e164_number}")
    if not any(e164_number.startswith(code) for code in allowed):
        allowed_str = ", ".join(sorted(allowed))
        raise ValueError(f"Destination number not in allowlist. Allowed: {allowed_str}.")


def _load_from_numbers() -> None:
    global _FROM_NUMBERS
    csv_str = os.getenv("FROM_NUMBERS", "").strip()
    if csv_str:
        nums = [normalize_to_e164(p) for p in csv_str.split(",") if p.strip()]
        _FROM_NUMBERS = nums


def _select_from_number_random() -> str:
    """
    Always choose a random FROM number if a pool is provided; otherwise use FROM_NUMBER.
    Cooldowns and 'prefer local' are intentionally ignored to meet the requirement.
    """
    if _FROM_NUMBERS:
        return random.choice(_FROM_NUMBERS)
    return os.getenv("FROM_NUMBER", "")


def start_flask_server(listen_host: str, listen_port: int) -> None:
    # Flask dev server supports websockets via flask-sock/simple-websocket
    app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False)


def ensure_public_base_url() -> Tuple[str, Optional[threading.Thread]]:
    global _PUBLIC_BASE_URL
    listen_host = os.getenv("LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.getenv("LISTEN_PORT", "5005"))
    public_base_url = os.getenv("PUBLIC_BASE_URL", None)
    use_ngrok = os.getenv("USE_NGROK", "false").strip().lower() in {"1", "true", "yes", "on"}

    server_thread = threading.Thread(target=start_flask_server, args=(listen_host, listen_port), daemon=True)
    server_thread.start()

    if public_base_url:
        public_base_url = public_base_url.rstrip("/")
        _PUBLIC_BASE_URL = public_base_url
        logging.info("Using PUBLIC_BASE_URL: %s", public_base_url)
        return public_base_url, server_thread

    if use_ngrok:
        if not _HAS_NGROK:
            raise RuntimeError("USE_NGROK=true but pyngrok is not installed. pip install pyngrok")
        authtoken = os.getenv("NGROK_AUTHTOKEN", None)
        ngrok_path = os.environ.get("NGROK_PATH", "")
        pyngrok_config = PyngrokConfig(ngrok_path=ngrok_path) if ngrok_path else None
        if authtoken:
            ngrok_conf.get_default().auth_token = authtoken
        http_tunnel = ngrok.connect(addr=listen_port, proto="http", bind_tls=True, pyngrok_config=pyngrok_config)
        public_url = http_tunnel.public_url.rstrip("/")
        _PUBLIC_BASE_URL = public_url
        logging.info("ngrok tunnel established: %s -> http://%s:%s", public_url, listen_host, listen_port)
        return public_url, server_thread

    raise RuntimeError("No PUBLIC_BASE_URL set and USE_NGROK is not enabled.")


def place_call(client: Client, url: str, from_number: str, to_number: str, interval_chosen_s: int, backoff_wait_s: int) -> str:
    create_kwargs: Dict[str, Any] = {
        "to": to_number,
        "from_": from_number,
        "url": url,
        "method": "POST",
        "status_callback": url.replace("/voice", "/status"),
        "status_callback_method": "POST",
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
    }
    if _ENABLE_AMD:
        create_kwargs["machine_detection"] = _AMD_MODE
        create_kwargs["machine_detection_timeout"] = _AMD_TIMEOUT_S
    if _recording_enabled():
        create_kwargs["record"] = True
        create_kwargs["recording_channels"] = "dual" if _RECORDING_CHANNELS == "dual" else "mono"
        create_kwargs["recording_status_callback"] = url.replace("/voice", "/recording-status")
        create_kwargs["recording_status_callback_method"] = "POST"
        create_kwargs["recording_status_callback_event"] = _RECORDING_STATUS_EVENTS or ["completed"]

    call = client.calls.create(**create_kwargs)
    diag = _ensure_diag_state(call.sid)
    with _diag_lock:
        diag["from_number"] = from_number
        diag["interval_chosen_s"] = interval_chosen_s
        diag["backoff_applied_s"] = backoff_wait_s
    logging.info("Call initiated. SID=%s To=%s From=%s interval_chosen=%ss backoff_applied=%ss", call.sid, to_number, from_number, interval_chosen_s, backoff_wait_s)
    return call.sid


# ====== Prompt rotation ======

def _format_prompt_text(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return raw.format(topic=_TOPIC, company=_COMPANY_NAME)
    except Exception:
        return raw


def _default_prompt_text() -> str:
    return f"Thanks for taking my call. I need help with my car. Could you help with { _TOPIC }?"


def _next_prompt() -> str:
    # Check for manual greeting phrase first (one-time use)
    manual_greeting = _consume_manual_greeting()
    if manual_greeting:
        logging.info("Using manual greeting phrase: %s", manual_greeting)
        return manual_greeting
    
    if not _ROTATE_PROMPTS:
        return _default_prompt_text()
    local_list = _prompts_list if _prompts_list else []
    if not local_list:
        return _default_prompt_text()

    global _prompt_index, _last_random_index
    with _prompt_lock:
        if _ROTATE_PROMPTS_STRATEGY == "random":
            if len(local_list) == 1:
                idx = 0
            else:
                choices = list(range(len(local_list)))
                if _last_random_index is not None and _last_random_index in choices and len(choices) > 1:
                    choices.remove(_last_random_index)
                idx = random.choice(choices)
                _last_random_index = idx
        else:
            idx = _prompt_index % len(local_list)
            _prompt_index = (idx + 1) % len(local_list)
    return _format_prompt_text(local_list[idx])


def _assign_prompt_if_needed(call_sid: str) -> str:
    d = _ensure_diag_state(call_sid)
    with _diag_lock:
        if not d.get("prompt"):
            d["prompt"] = _next_prompt()
        return d["prompt"]


# ====== TwiML endpoints ======

@app.route("/voice", methods=["POST"])
def voice_entrypoint() -> Response:
    call_sid = request.form.get("CallSid", "")
    _ensure_call_state(call_sid)
    _schedule_forced_hangup(call_sid, seconds=_MAX_CALL_DURATION_S)

    # Assign a prompt to this call at the very start
    assigned_prompt = _assign_prompt_if_needed(call_sid)
    logging.info("Prompt selected for CallSid=%s: %s", call_sid, assigned_prompt or "<empty>")

    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/wait_for_callee?cycle=1' "
        f"timeout='{max(1, min(10, _GREETING_WAIT_TIMEOUT_S))}' "
        f"speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
        f"actionOnEmptyResult='true' "
        f"partialResultCallback='/transcribe-partial?stage=hello&amp;seq=1' "
        f"partialResultCallbackMethod='POST'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/wait_for_callee", methods=["POST"])
def wait_for_callee_handler() -> Response:
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    cycle = int(request.args.get("cycle", "1") or "1")

    # Include Media Stream start only once (on first cycle) to avoid duplicates.
    # Stream inbound (callee) audio to our WebSocket endpoint.
    wss_base = ""
    if _PUBLIC_BASE_URL:
        wss_base = _PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    stream_start = ""
    if cycle == 1 and wss_base:
        stream_start = f"<Start><Stream url='{_escape_xml(wss_base + '/media-stream')}' track='inbound_track' /></Start>"

    if speech_text:
        _append_callee_line(call_sid, speech_text)
    else:
        if cycle < _GREETING_MAX_CYCLES:
            next_cycle = cycle + 1
            xml = (
                "<Response>"
                f"{stream_start}"
                f"<Gather input='speech' method='POST' action='/wait_for_callee?cycle={next_cycle}' "
                f"timeout='{max(1, min(10, _GREETING_WAIT_TIMEOUT_S))}' "
                f"speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
                f"actionOnEmptyResult='true' "
                f"partialResultCallback='/transcribe-partial?stage=hello&amp;seq={next_cycle}' "
                f"partialResultCallbackMethod='POST'/>"
                "</Response>"
            )
            return twiml_response(xml)

    with _call_state_lock:
        st = _call_state.get(call_sid)
        if st:
            st["fsm_state"] = "GREET_AND_PROMPT"

    # Compose initial lines: greeting + optional consent + rotating prompt.
    prompt_line = _assign_prompt_if_needed(call_sid) or _default_prompt_text()
    lines = []
    lines.append(f"Hello. This is an automated assistant from {_COMPANY_NAME}.")
    if _recording_enabled():
        lines.append("With your consent, this call may be recorded for quality and support.")
    lines.append(prompt_line)
    for ln in lines:
        _append_assistant_line(call_sid, ln)

    say_voice = _escape_xml(_TTS_VOICE)
    say_lang = _escape_xml(_TTS_LANGUAGE)
    parts: List[str] = ["<Response>"]
    if stream_start:
        parts.append(stream_start)
    parts.append(
        f"<Gather input='speech' method='POST' action='/transcribe?seq=1' "
        f"timeout='{_CALLEE_SILENCE_HANGUP_S}' speechTimeout='auto' language='{say_lang}' actionOnEmptyResult='true' "
        f"bargeIn='true' partialResultCallback='/transcribe-partial?stage=dialog&amp;seq=1' "
        f"partialResultCallbackMethod='POST'>"
    )
    for ln in lines:
        parts.append(f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(ln)}</Say>")
        parts.append("<Pause length='0.4'/>")
    parts.append("</Gather>")
    parts.append("</Response>")
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if st:
            st["fsm_state"] = "DIALOG"
    return twiml_response("".join(parts))


@app.route("/transcribe-partial", methods=["POST"])
def transcribe_partial_handler() -> Response:
    call_sid = request.form.get("CallSid", "")
    partial = request.form.get("UnstableSpeechResult") or request.form.get("SpeechResult") or ""
    if partial:
        _append_partial_callee(call_sid, partial)
        _buffer_partial(call_sid, partial, inactivity_ms=1000)
    return Response("", status=204)


@app.route("/transcribe", methods=["POST"])
def transcribe_handler() -> Response:
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    seq = int(request.args.get("seq", "1") or "1")

    if speech_text:
        _clear_live_utterance(call_sid)
        _append_callee_line(call_sid, speech_text)

        with _call_state_lock:
            st = _call_state.get(call_sid)
            context = st["context"] if st else {}

        next_lines, should_end = _next_assistant_turn(context, speech_text)
        if should_end:
            say_voice = _escape_xml(_TTS_VOICE)
            say_lang = _escape_xml(_TTS_LANGUAGE)
            line = (next_lines or ["Understood. Thank you."])[0]
            _append_assistant_line(call_sid, line)
            xml = f"<Response><Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(line)}</Say><Hangup/></Response>"
            with _call_state_lock:
                st2 = _call_state.get(call_sid)
                if st2:
                    st2["fsm_state"] = "ENDING"
            return twiml_response(xml)

        next_seq = seq + 1
        if next_lines:
            for ln in next_lines:
                _append_assistant_line(call_sid, ln)
            say_voice = _escape_xml(_TTS_VOICE)
            say_lang = _escape_xml(_TTS_LANGUAGE)
            parts: List[str] = ["<Response>"]
            parts.append(
                f"<Gather input='speech' method='POST' action='/transcribe?seq={next_seq}' "
                f"timeout='{_CALLEE_SILENCE_HANGUP_S}' speechTimeout='auto' language='{say_lang}' actionOnEmptyResult='true' "
                f"bargeIn='true' partialResultCallback='/transcribe-partial?stage=dialog&amp;seq={next_seq}' "
                f"partialResultCallbackMethod='POST'>"
            )
            for ln in next_lines:
                parts.append(f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(ln)}</Say>")
                parts.append("<Pause length='0.4'/>")
            parts.append("</Gather>")
            parts.append("</Response>")
            return twiml_response("".join(parts))
        else:
            say_lang = _escape_xml(_TTS_LANGUAGE)
            xml = (
                "<Response>"
                f"<Gather input='speech' method='POST' action='/transcribe?seq={next_seq}' "
                f"timeout='{_CALLEE_SILENCE_HANGUP_S}' speechTimeout='auto' language='{say_lang}' "
                f"actionOnEmptyResult='true' bargeIn='true' "
                f"partialResultCallback='/transcribe-partial?stage=dialog&amp;seq={next_seq}' "
                f"partialResultCallbackMethod='POST'/>"
                "</Response>"
            )
            return twiml_response(xml)

    with _call_state_lock:
        st = _call_state.get(call_sid)
        if st:
            st["fsm_state"] = "ENDING"
    return twiml_response("<Response><Hangup/></Response>")


def _next_assistant_turn(context: Dict[str, Any], user_text: str) -> Tuple[Optional[List[str]], bool]:
    t = (user_text or "").lower()
    if any(k in t for k in ["do not call", "remove me", "stop calling", "unsubscribe", "no more calls", "wrong number"]):
        return (["Understood. We will not call again. Thank you for your time."], True)

    if not (context.get("year") and context.get("make") and context.get("model")):
        return (["Could you share the year, make, and model?"], False)
    if not context.get("engine"):
        return (["Do you know the engine size or the 8th digit of the VIN?"], False)
    if not context.get("location"):
        return (["What is your location for logistics?"], False)
    if not context.get("budget"):
        return (["What budget range should we consider for parts and labor?"], False)
    return (["If that works, what is the next step to move forward?"], False)


# ====== Live audio WebSockets ======

@sock.route("/media-stream")
def media_stream(ws):
    """
    Twilio connects here (wss) and sends JSON events:
    - {"event":"start", ...}
    - {"event":"media", "media":{"payload": "<base64 mu-law 8k audio>"}}
    - {"event":"stop"}
    We rebroadcast 'media' payload to all connected browser listeners.
    """
    call_sid = None
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            try:
                data = json.loads(msg)
            except Exception:
                continue
            ev = (data.get("event") or "").lower()
            if ev == "start":
                start = data.get("start", {})
                call_sid = start.get("callSid") or start.get("streamSid")
                logging.info("Media stream started: %s", call_sid or "<unknown>")
            elif ev == "media":
                media = data.get("media", {})
                payload = media.get("payload")
                if payload:
                    # Broadcast only the audio payload; browser JS decodes and plays.
                    out = json.dumps({"type": "media", "payload": payload})
                    with _audio_clients_lock:
                        dead = []
                        for cli in _audio_clients:
                            try:
                                cli.send(out)
                            except Exception:
                                dead.append(cli)
                        for cli in dead:
                            _audio_clients.discard(cli)
            elif ev == "stop":
                logging.info("Media stream stopped: %s", call_sid or "<unknown>")
                break
    except Exception as e:
        logging.error("Media stream error: %s", str(e))
    finally:
        try:
            ws.close()
        except Exception:
            pass


@sock.route("/ws/live-audio")
def ws_live_audio(ws):
    """
    Browsers connect here to receive live audio frames (JSON with base64 mu-law).
    """
    with _audio_clients_lock:
        _audio_clients.add(ws)
    try:
        while True:
            # Keep-alive; we do not expect messages from clients. Close on disconnect.
            msg = ws.receive()
            if msg is None:
                break
    except Exception:
        pass
    finally:
        with _audio_clients_lock:
            _audio_clients.discard(ws)
        try:
            ws.close()
        except Exception:
            pass


# ====== Status, recording, and persistence ======

def _history_csv_headers() -> List[str]:
    return ["callSid", "startedAt", "durationSec", "outcome", "transcript", "prompt"]


def _ensure_history_dir() -> None:
    directory = os.path.dirname(_HISTORY_CSV_PATH)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)


def _history_load_from_csv() -> None:
    global _history, _persisted_call_sids
    path = _HISTORY_CSV_PATH
    if not os.path.isfile(path):
        return
    try:
        with _history_file_lock, open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        loaded: List[Dict[str, Any]] = []
        for r in rows:
            call_sid = r.get("callSid", "").strip()
            if not call_sid:
                continue
            try:
                started_at = int(r.get("startedAt") or "0")
            except Exception:
                started_at = 0
            try:
                duration_sec = int(r.get("durationSec") or "0")
            except Exception:
                duration_sec = 0
            outcome = r.get("outcome", "") or ""
            transcript_json = r.get("transcript", "") or "[]"
            try:
                transcript = json.loads(transcript_json)
                if not isinstance(transcript, list):
                    transcript = []
            except Exception:
                transcript = []
            prompt_val = r.get("prompt", "") or ""
            loaded.append({
                "callSid": call_sid,
                "startedAt": started_at,
                "durationSec": duration_sec,
                "outcome": outcome,
                "transcript": transcript,
                "prompt": prompt_val,
            })
        with _history_lock:
            _history = loaded
        _persisted_call_sids = {h["callSid"] for h in loaded if h.get("callSid")}
        logging.info("Loaded %s historical call(s) from %s", len(loaded), path)
    except Exception as e:
        logging.error("Failed to load history CSV (%s): %s", path, str(e))


def _history_append_to_csv(entry: Dict[str, Any]) -> None:
    call_sid = entry.get("callSid", "")
    if not call_sid:
        return

    with _history_file_lock:
        if call_sid in _persisted_call_sids:
            return
        _ensure_history_dir()
        path = _HISTORY_CSV_PATH
        file_exists = os.path.isfile(path)
        try:
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_history_csv_headers())
                if not file_exists or os.path.getsize(path) == 0:
                    writer.writeheader()
                writer.writerow({
                    "callSid": entry.get("callSid", ""),
                    "startedAt": int(entry.get("startedAt", 0) or 0),
                    "durationSec": int(entry.get("durationSec", 0) or 0),
                    "outcome": entry.get("outcome", "") or "",
                    "transcript": json.dumps(entry.get("transcript", []), ensure_ascii=False),
                    "prompt": entry.get("prompt", "") or "",
                })
            _persisted_call_sids.add(call_sid)
        except Exception as e:
            logging.error("Failed to append history to CSV: %s", str(e))


@app.route("/status", methods=["POST"])
def status_handler() -> Response:
    call_sid = request.form.get("CallSid", "")
    raw_event = request.form.get("StatusCallbackEvent", "") or ""
    call_status = request.form.get("CallStatus", "") or ""
    answered_by = request.form.get("AnsweredBy", "")
    sip_code = request.form.get("SipResponseCode", "")
    call_duration = request.form.get("CallDuration", "")

    event = (raw_event or call_status or "").lower()

    if call_sid:
        _record_event(call_sid, event=event, call_status=call_status, answered_by=answered_by or None, sip_code=sip_code or None, duration=call_duration or None)

    with _diag_lock:
        diag = _call_diag.get(call_sid, {})
        from_number = diag.get("from_number")
        interval_chosen = diag.get("interval_chosen_s")
        backoff_applied = diag.get("backoff_applied_s")
        selected_prompt = diag.get("prompt") or ""

    logging.info(
        "Status callback: event=%s call_status=%s answered_by=%s sip_code=%s duration=%s CallSid=%s from=%s interval=%s backoff=%s",
        event or "<none>", call_status or "<none>", answered_by or "<none>", sip_code or "<none>", call_duration or "<none>",
        call_sid or "<none>", from_number or "<none>", str(interval_chosen or ""), str(backoff_applied or "")
    )

    terminal_statuses = {"completed", "busy", "no-answer", "failed", "canceled"}
    is_terminal = (event == "completed") or (call_status.lower() in terminal_statuses)

    if is_terminal:
        outcome = _classify_outcome(call_sid)

        try:
            with _call_state_lock:
                st = _call_state.get(call_sid)
                segments = list(st["segments"]) if st else []
                started_at = float(st["start_ts"]) if st and st.get("start_ts") else time.time()
            transcript_msgs: List[Dict[str, Any]] = []
            now_ts = int(time.time())
            for seg in segments:
                role = "Assistant" if seg.startswith("Assistant: ") else ("Callee" if seg.startswith("Callee: ") else "System")
                text = seg.split(": ", 1)[1] if ": " in seg else seg
                transcript_msgs.append({"role": role, "text": text, "ts": now_ts})
            entry = {
                "callSid": call_sid,
                "startedAt": int(started_at),
                "durationSec": int(call_duration or "0") if (call_duration or "").isdigit() else 0,
                "outcome": outcome,
                "transcript": transcript_msgs,
                "prompt": selected_prompt or "",
            }
            with _history_lock:
                _history.append(entry)
            _history_append_to_csv(entry)
        except Exception as e:
            logging.error("Failed to persist call history: %s", str(e))

        to_number = request.form.get("To", "")
        if to_number:
            _update_backoff(to_number, outcome)

        _cancel_forced_hangup(call_sid)
        _finalize_transcript(call_sid)

        with _call_state_lock:
            st = _call_state.pop(call_sid, None)
            if st and st.get("live_utterance", {}).get("inactivity_timer"):
                try:
                    st["live_utterance"]["inactivity_timer"].cancel()
                except Exception:
                    pass
        with _diag_lock:
            _call_diag.pop(call_sid, None)

    return Response("", status=204)


@app.route("/recording-status", methods=["POST"])
def recording_status_handler() -> Response:
    call_sid = request.form.get("CallSid", "")
    rec_sid = request.form.get("RecordingSid", "")
    rec_status = request.form.get("RecordingStatus", "")
    rec_url = request.form.get("RecordingUrl", "")
    rec_channels = request.form.get("RecordingChannels", "")
    rec_source = request.form.get("RecordingSource", "")

    if call_sid and rec_sid:
        d = _ensure_diag_state(call_sid)
        with _diag_lock:
            if rec_url:
                d["recording_urls"].append(rec_url)
            d["recording_sids"].append(rec_sid)

    logging.info(
        "Recording callback: CallSid=%s RecordingSid=%s Status=%s Channels=%s Source=%s URL=%s",
        call_sid or "<none>", rec_sid or "<none>", rec_status or "<none>", rec_channels or "<none>", rec_source or "<none>", rec_url or "<none>"
    )

    return Response("", status=204)


# ====== Admin Functions ======

def _get_client_ip() -> str:
    """Get client IP address from request"""
    return request.environ.get('HTTP_X_FORWARDED_FOR', request.environ.get('REMOTE_ADDR', ''))

def _is_admin_locked_out(ip: str) -> bool:
    """Check if admin IP is locked out due to failed attempts"""
    now = time.time()
    lockout_until = _admin_lockout_until.get(ip, 0)
    return now < lockout_until

def _record_admin_failure(ip: str) -> None:
    """Record a failed admin login attempt"""
    now = time.time()
    attempts = _admin_failed_attempts.setdefault(ip, [])
    # Keep only attempts from last 5 minutes
    attempts[:] = [t for t in attempts if t > now - 300]
    attempts.append(now)
    
    # Lock out after 5 failed attempts for 5 minutes
    if len(attempts) >= 5:
        _admin_lockout_until[ip] = now + 300  # 5 minutes

def _clear_admin_failures(ip: str) -> None:
    """Clear failed attempts for successful login"""
    _admin_failed_attempts.pop(ip, None)
    _admin_lockout_until.pop(ip, None)

def _is_secret_env_var(key: str) -> bool:
    """Check if environment variable key contains secret patterns"""
    key_lower = key.lower()
    return any(pattern in key_lower for pattern in _SECRET_PATTERNS)

def _get_safe_env_vars() -> Dict[str, str]:
    """Get environment variables that are safe to edit (exclude secrets)"""
    safe_vars = {}
    for key, value in os.environ.items():
        if not _is_secret_env_var(key):
            safe_vars[key] = value
    return safe_vars

def _update_env_file(updates: Dict[str, str]) -> None:
    """Update .env file with new values"""
    env_path = Path(".env")
    
    # Read existing .env file
    existing_lines = []
    existing_vars = {}
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            existing_lines = f.read().splitlines()
        
        for line in existing_lines:
            if '=' in line and not line.strip().startswith('#'):
                key, value = line.split('=', 1)
                existing_vars[key.strip()] = value

    # Update with new values (only safe vars)
    for key, value in updates.items():
        if not _is_secret_env_var(key):
            existing_vars[key] = value

    # Write back to .env file
    with open(env_path, 'w', encoding='utf-8') as f:
        for key, value in existing_vars.items():
            f.write(f"{key}={value}\n")

def _is_admin() -> bool:
    """Check if current session is authenticated as admin"""
    return session.get('is_admin') is True

def _consume_manual_greeting() -> Optional[str]:
    """Get and clear the manual greeting phrase (one-time use)"""
    global _manual_greeting_phrase
    with _greeting_lock:
        phrase = _manual_greeting_phrase
        _manual_greeting_phrase = None
        return phrase


# ====== Web UI pages and favicon ======

@app.route("/scamcalls", methods=["GET"])
def ui_live_page() -> Response:
    return render_template("scamcalls.html")


@app.route("/scamcalls/history", methods=["GET"])
def ui_history_page() -> Response:
    return render_template("scamcalls_history.html")


@app.route("/favicon.ico")
def favicon_compat() -> Response:
    return send_from_directory(os.path.join(app.root_path, "static"), "favicon.ico", mimetype="image/x-icon")


# ====== Admin API Endpoints ======

@app.route("/api/admin/login", methods=["POST"])
def api_admin_login() -> Response:
    """Admin login endpoint"""
    ip = _get_client_ip()
    
    # Check if IP is locked out
    if _is_admin_locked_out(ip):
        return jsonify({"error": "Too many failed attempts. Try again later."}), 429
    
    try:
        data = request.get_json() or {}
        username = data.get('username', '')
        password = data.get('password', '')
        
        if username == _ADMIN_USERNAME and password == _ADMIN_PASSWORD:
            _clear_admin_failures(ip)
            session['is_admin'] = True
            session.permanent = True
            return jsonify({"ok": True})
        else:
            _record_admin_failure(ip)
            return jsonify({"error": "Invalid credentials"}), 401
            
    except Exception as e:
        logging.error("Admin login error: %s", str(e))
        return jsonify({"error": "Login failed"}), 500

@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout() -> Response:
    """Admin logout endpoint"""
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/admin/config", methods=["GET"])
def api_admin_config_get() -> Response:
    """Get safe environment variables for admin editing"""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401
    
    safe_vars = _get_safe_env_vars()
    return jsonify(safe_vars)

@app.route("/api/admin/config", methods=["POST"])
def api_admin_config_post() -> Response:
    """Update environment variables"""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.get_json() or {}
        updates = data.get('updates', {})
        
        # Only allow updates to safe variables
        safe_updates = {k: v for k, v in updates.items() if not _is_secret_env_var(k)}
        
        _update_env_file(safe_updates)
        return jsonify({"ok": True})
        
    except Exception as e:
        logging.error("Admin config update error: %s", str(e))
        return jsonify({"error": "Update failed"}), 500

@app.route("/api/scamcalls/set-greeting", methods=["POST"])
def api_set_greeting() -> Response:
    """Set manual greeting phrase for next call"""
    try:
        data = request.get_json() or {}
        phrase = data.get('phrase', '').strip()
        
        if not phrase:
            return jsonify({"error": "Phrase cannot be empty"}), 400
        
        # Validate word count (5-15 words)
        word_count = len(phrase.split())
        if word_count < 5 or word_count > 15:
            return jsonify({"error": "Phrase must be 5-15 words"}), 400
        
        global _manual_greeting_phrase
        with _greeting_lock:
            _manual_greeting_phrase = phrase
        
        return jsonify({"ok": True})
        
    except Exception as e:
        logging.error("Set greeting error: %s", str(e))
        return jsonify({"error": "Failed to set greeting"}), 500

@app.route("/api/scamcalls/reload-now", methods=["POST"])
def api_reload_now() -> Response:
    """Request application reload"""
    global _reload_requested
    with _reload_status_lock:
        _reload_requested = True
    
    # In a production app, this would trigger a graceful restart
    # For now, we just set a flag that can be checked
    logging.info("Application reload requested")
    return jsonify({"ok": True, "message": "Reload requested"})

@app.route("/api/scamcalls/reload-status", methods=["GET"])
def api_reload_status() -> Response:
    """Get reload status"""
    with _reload_status_lock:
        status = "pending" if _reload_requested else "ready"
    return jsonify({"status": status})


# ====== Web UI APIs ======

def _format_active_window_label() -> str:
    days = "–".join([_ACTIVE_DAYS[0], _ACTIVE_DAYS[-1]]) if _ACTIVE_DAYS else ""
    return f"{days} {_ACTIVE_HOURS_LOCAL}" if days else _ACTIVE_HOURS_LOCAL


def _current_active_call() -> Optional[str]:
    with _call_state_lock:
        active = [(sid, st) for sid, st in _call_state.items() if not st.get("closed", False)]
        if not active:
            return None
        active.sort(key=lambda kv: kv[1].get("start_ts", 0.0), reverse=True)
        return active[0][0]


def _segments_to_messages(call_sid: str, include_partial: bool = True) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    with _call_state_lock:
        st = _call_state.get(call_sid)
        if not st:
            return messages
        segs = list(st.get("segments", []))
        live_buf = st.get("live_utterance", {}).get("buffer", "")
    for seg in segs:
        if seg.startswith("Assistant: "):
            messages.append({"role": "Assistant", "text": seg[len("Assistant: "):], "ts": now_ts})
        elif seg.startswith("Callee: "):
            messages.append({"role": "Callee", "text": seg[len("Callee: "):], "ts": now_ts})
        else:
            messages.append({"role": "System", "text": seg, "ts": now_ts})
    if include_partial and live_buf:
        messages.append({"role": "Callee", "text": live_buf, "partial": True, "ts": now_ts})
    return messages


@app.route("/api/scamcalls/status", methods=["GET"])
def api_status() -> Response:
    active_sid = _current_active_call()
    
    # Include manual greeting if set
    next_greeting = None
    with _greeting_lock:
        next_greeting = _manual_greeting_phrase
    
    data = {
        "active": bool(active_sid),
        "callSid": active_sid,
        "nextCallEpochSec": int(_next_call_epoch_s) if _next_call_epoch_s else None,
        "nextCallStartEpochSec": int(_next_call_start_epoch_s) if _next_call_start_epoch_s else None,
        "destNumber": _DEST_NUMBER or "",
        "fromNumber": _last_from_number or "",
        "activeWindow": _format_active_window_label(),
        "caps": {"hourly": _HOURLY_MAX_PER_DEST, "daily": _DAILY_MAX_PER_DEST},
        "publicUrl": _PUBLIC_BASE_URL or "",
        "nextGreeting": next_greeting,
    }
    return jsonify(data)


@app.route("/api/scamcalls/active", methods=["GET"])
def api_active() -> Response:
    active_sid = _current_active_call()
    if not active_sid:
        return jsonify({"status": "idle"}), 200
    messages = _segments_to_messages(active_sid, include_partial=True)
    with _call_state_lock:
        st = _call_state.get(active_sid)
        connected_at = int(st.get("start_ts", time.time())) if st else int(time.time())
        status = "connected" if st and not st.get("closed", False) else "completed"
    return jsonify({
        "callSid": active_sid,
        "connectedAt": connected_at,
        "transcript": messages,
        "status": status,
    })


@app.route("/api/scamcalls/history", methods=["GET"])
def api_history() -> Response:
    with _history_lock:
        calls = [{"callSid": h["callSid"],
                  "startedAt": h.get("startedAt", 0),
                  "durationSec": h.get("durationSec", 0),
                  "outcome": h.get("outcome", "")}
                 for h in _history]
    return jsonify({"calls": calls, "publicUrl": _PUBLIC_BASE_URL or ""})


@app.route("/api/scamcalls/transcript/<call_sid>", methods=["GET"])
def api_transcript(call_sid: str) -> Response:
    with _history_lock:
        for h in _history:
            if h.get("callSid") == call_sid:
                return jsonify({"callSid": call_sid,
                                "transcript": h.get("transcript", []),
                                "outcome": h.get("outcome", ""),
                                "durationSec": h.get("durationSec", 0)})
    with _call_state_lock:
        if call_sid in _call_state:
            msgs = _segments_to_messages(call_sid, include_partial=True)
            st = _call_state[call_sid]
            return jsonify({"callSid": call_sid,
                            "transcript": msgs,
                            "outcome": "",
                            "durationSec": int(time.time() - st.get("start_ts", time.time()))})
    return jsonify({"error": "CallSid not found"}), 404


@app.route("/api/scamcalls/call-now", methods=["POST"])
def api_call_now() -> Response:
    # Check if call can be attempted now (caps/backoff)
    if _DEST_NUMBER:
        allowed, wait_time = _can_attempt(time.time(), _DEST_NUMBER)
        if not allowed:
            return jsonify({
                "error": "Max calls reached in allotted time.",
                "code": "cap",
                "waitSeconds": wait_time
            }), 429
    
    _manual_call_requested.set()
    logging.info("Call-now requested via API.")
    return jsonify({"ok": True}), 200


# ====== CLI setup and main loop ======

def _sleep_with_manual_wake(wait_s: int) -> bool:
    slept = 0
    while slept < wait_s and not _STOP_REQUESTED:
        if _manual_call_requested.is_set():
            logging.info("Manual 'Call now' request received; interrupting wait.")
            return True
        time.sleep(min(1, wait_s - slept))
        slept += 1
    return False


def _readline_with_timeout(prompt: str, timeout_s: int) -> Optional[str]:
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
    except Exception:
        return None

    sys.stdout.write(prompt)
    sys.stdout.flush()

    try:
        import select
        rlist, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if rlist:
            line = sys.stdin.readline()
            return line.rstrip("\n")
        else:
            return None
    except Exception:
        return None


def interactive_setup(current_to: str) -> Tuple[str, bool]:
    """
    Minimal interactive setup with timeout. Defaults used if no TTY or no input within 10s.
    """
    if _NONINTERACTIVE:
        return current_to, bool(_FROM_NUMBERS)

    try:
        if not sys.stdin or not sys.stdin.isatty():
            logging.info("No TTY detected. Proceeding with defaults (non-interactive).")
            return current_to, bool(_FROM_NUMBERS)
    except Exception:
        logging.info("Unable to determine TTY. Proceeding with defaults (non-interactive).")
        return current_to, bool(_FROM_NUMBERS)

    print("Run mode: calls (voice) only. SMS is not implemented in this version.")
    print(f"Current destination: {current_to}")

    override = _readline_with_timeout("Enter a destination E.164 number to override, or press Enter to keep (10s): ", 10)
    if override is None:
        print("\nNo input received within 10s; keeping current destination.")
    elif override.strip():
        try:
            current_to = normalize_to_e164(override.strip())
        except Exception as e:
            print(f"Invalid number; keeping existing. Error: {e}")

    # Rotation flag not used; random selection is always applied when pool exists.
    return current_to, bool(_FROM_NUMBERS)


def main() -> int:
    global _DEST_NUMBER, _last_from_number, _next_call_epoch_s, _next_call_start_epoch_s
    setup_logging()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    # Load persisted history at startup before serving UI
    _history_load_from_csv()

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    raw_from = os.getenv("FROM_NUMBER", "")
    raw_to = os.getenv("TO_NUMBER", "")

    if not all([account_sid, auth_token]) or (not raw_from and not os.getenv("FROM_NUMBERS", "").strip()) or not raw_to:
        logging.error("Missing required environment variables. Ensure TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TO_NUMBER and either FROM_NUMBER or FROM_NUMBERS are set.")
        return 2

    try:
        allowed_cc = parse_allowed_country_codes(os.getenv("ALLOWED_COUNTRY_CODES", "+1"))
    except ValueError as e:
        logging.error(str(e))
        return 2

    try:
        _load_from_numbers()
    except Exception as e:
        logging.error("Failed to load FROM_NUMBERS: %s", str(e))
        return 2

    try:
        if _FROM_NUMBERS:
            for fn in _FROM_NUMBERS:
                enforce_country_allowlist(fn, allowed_cc)
        else:
            raw_from = normalize_to_e164(raw_from)
            enforce_country_allowlist(raw_from, allowed_cc)
        to_number = normalize_to_e164(raw_to)
        enforce_country_allowlist(to_number, allowed_cc)
    except ValueError as e:
        logging.error(str(e))
        return 2

    _DEST_NUMBER = to_number

    _, _rotate_pool = interactive_setup(to_number)

    try:
        public_base_url, server_thread = ensure_public_base_url()
    except Exception as e:
        logging.error("Failed to establish public URL: %s", str(e))
        return 2

    global _TWILIO_CLIENT
    http_client = TwilioHttpClient(timeout=30)
    _TWILIO_CLIENT = Client(account_sid, auth_token, http_client=http_client)

    logging.info(
        "Starting call loop. MIN_INTERVAL=%ss, MAX_INTERVAL=%ss. Hourly cap=%s, Daily cap=%s. Active days=%s hours=%s. AMD=%s(%s/%ss), Recording=%s(%s). FROM pool size=%s",
        _MIN_INTERVAL_S, _MAX_INTERVAL_S, _HOURLY_MAX_PER_DEST, _DAILY_MAX_PER_DEST, ",".join(_ACTIVE_DAYS), _ACTIVE_HOURS_LOCAL,
        "on" if _ENABLE_AMD else "off", _AMD_MODE, _AMD_TIMEOUT_S,
        "on" if _recording_enabled() else "off", _RECORDING_CHANNELS, len(_FROM_NUMBERS)
    )

    calls_made = 0
    while not _STOP_REQUESTED:
        if _manual_call_requested.is_set():
            _manual_call_requested.clear()
            logging.info("Processing manual 'Call now' request.")

        now_dt = _now_local()

        wait_active = _time_until_active_window(now_dt)
        if wait_active > 0:
            now_ts = int(time.time())
            _next_call_start_epoch_s = now_ts
            _next_call_epoch_s = now_ts + wait_active
            logging.info("Outside active window. Sleeping %ss until next window.", wait_active)
            if _sleep_with_manual_wake(wait_active):
                continue
            else:
                continue

        allowed, wait_caps = _can_attempt(time.time(), to_number)
        if not allowed:
            now_ts = int(time.time())
            _next_call_start_epoch_s = now_ts
            _next_call_epoch_s = now_ts + wait_caps
            logging.info("Attempt caps/backoff prevent calling now. Sleeping %ss.", wait_caps)
            if _sleep_with_manual_wake(wait_caps):
                continue
            else:
                continue

        # Randomized interval for the next attempt after this call
        interval_chosen = random.randint(_MIN_INTERVAL_S, _MAX_INTERVAL_S)

        # Always choose a random FROM number when a pool is provided
        from_number = _select_from_number_random()
        _last_from_number = from_number

        msg_url = f"{public_base_url}/voice"

        try:
            _ = place_call(_TWILIO_CLIENT, url=msg_url, from_number=from_number, to_number=to_number, interval_chosen_s=interval_chosen, backoff_wait_s=0)
        except TwilioRestException as e:
            logging.error("Twilio API error placing call (status %s, code %s): %s", getattr(e, "status", None), getattr(e, "code", None), str(e))
            wait_err = min(180, max(30, _MIN_INTERVAL_S // 2))
            now_ts = int(time.time())
            _next_call_start_epoch_s = now_ts
            _next_call_epoch_s = now_ts + wait_err
            logging.info("Retrying after %ss due to Twilio API error.", wait_err)
            if _sleep_with_manual_wake(wait_err):
                continue
            continue
        except Exception as e:
            is_requests_err = requests is not None and isinstance(e, requests.exceptions.RequestException)
            if is_requests_err or "Temporary failure in name resolution" in str(e):
                logging.error("Network error placing call (likely transient): %s", str(e))
                wait_err = min(180, max(30, _MIN_INTERVAL_S // 2))
                now_ts = int(time.time())
                _next_call_start_epoch_s = now_ts
                _next_call_epoch_s = now_ts + wait_err
                logging.info("Retrying after %ss due to network error.", wait_err)
                if _sleep_with_manual_wake(wait_err):
                    continue
                continue
            logging.exception("Unexpected error placing call: %s", str(e))
            wait_err = 60
            now_ts = int(time.time())
            _next_call_start_epoch_s = now_ts
            _next_call_epoch_s = now_ts + wait_err
            if _sleep_with_manual_wake(wait_err):
                continue
            continue

        with _attempts_lock:
            _dest_attempts.setdefault(to_number, []).append(time.time())
        calls_made += 1

        now_ts = int(time.time())
        _next_call_start_epoch_s = now_ts
        _next_call_epoch_s = now_ts + interval_chosen

        if _sleep_with_manual_wake(interval_chosen):
            continue

    logging.info("Stopped. Total calls attempted: %s", calls_made)
    return 0


@app.route("/greet", methods=["POST"])
def greet_handler_legacy() -> Response:
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    if speech_text:
        _append_callee_line(call_sid, speech_text)
    return wait_for_callee_handler()


if __name__ == "__main__":
    raise SystemExit(main())
