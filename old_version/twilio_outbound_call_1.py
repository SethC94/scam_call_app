#!/usr/bin/env python3
"""
Twilio outbound caller (focused with call outcome diagnostics and full-conversation logging)

Objectives
- Place an outbound call at a configurable interval.
- Wait for a human greeting; confirm with a short speech listen loop.
- After a validated greeting, speak a brief greeting, pause, then ask an open-ended prompt (male voice).
- Log exactly what is spoken to the callee (assistant lines) and what the callee says (transcriptions) to reconstruct the full conversation.
- Optionally record the call audio via Twilio and log recording URLs when available.
- Keep the call open until the callee ends it or until 60 seconds have elapsed, whichever comes first.

Requirements
- Python 3.8+
- pip install twilio flask
- (optional) pip install python-dotenv  # auto-loads .env

Networking
- Twilio must reach a publicly accessible URL to fetch TwiML and to POST transcriptions and status/recording callbacks.
- Provide a public base URL via PUBLIC_BASE_URL.

Environment variables (required unless noted)
- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN
- FROM_NUMBER                  E.164, your Twilio voice number (e.g., +12095550123)
- TO_NUMBER                    E.164 destination number (e.g., +18319460285)

Scheduling
- CALL_INTERVAL_SECONDS        Default: 60 (minimum enforced)
- MAX_CALLS                    Default: 0 (0 means infinite loop)

Public URL configuration
- PUBLIC_BASE_URL              Example: "https://your-domain.example.com"
- LISTEN_HOST                  Default: "0.0.0.0"
- LISTEN_PORT                  Default: "5005"

Greeting detection
- GREETING_WAIT_TIMEOUT_SECONDS Default: 2  (1..10) duration of each short listen
- GREETING_MAX_CYCLES           Default: 3  maximum short-listen cycles before proceeding
- GREETING_KEYWORDS             Default: common greeting phrases (comma-separated, case-insensitive)

Voice and content
- TTS_VOICE                    Default: "man" (male voice)
- TTS_LANGUAGE                 Default: "en-US"
- COMPANY_NAME                 Default: "Your Company" (used in greeting)
- TOPIC                        Default: "your recent experience" (used in prompt formatting)

Answering Machine Detection (AMD)
- ENABLE_AMD                   Default: "true"  (Enable to detect human vs machine/voicemail)
- AMD_MODE                     Default: "Enable" (also "DetectMessageEnd" to wait for beep)
- AMD_TIMEOUT_SECONDS          Default: 20 (3..59)

Recording (optional)
- RECORD_CALLS                 "true"/"false" (default: "false")
- RECORDING_CHANNELS           "mono" or "dual" (default: "mono")
- RECORDING_STATUS_EVENTS      CSV of events (default: "in-progress,completed")

Logging
- LOG_COLOR                    Default: "1" (enable ANSI color/bold for transcription lines)

Compliance note
- Use only with consent and for lawful purposes. Provide any required disclosures.
"""

import os
import re
import sys
import time
import random
import signal
import logging
import threading
from typing import Optional, Tuple, Set, Dict, Any, List

# Optional .env auto-load (no-op if python-dotenv is not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from flask import Flask, request, Response

try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    from twilio.http.http_client import TwilioHttpClient
except Exception:
    print("Missing dependency: Install the Twilio SDK: pip install twilio", file=sys.stderr)
    raise

# Message prompts
try:
    from rotating_iv_prompts import PROMPTS as ROTATING_PROMPTS
except Exception:
    print("Missing rotating_iv_prompts.py or PROMPTS symbol. Ensure the file is present.", file=sys.stderr)
    raise

# Flask app
app = Flask(__name__)

# Globals and configuration
_STOP_REQUESTED = False
ANSI_BOLD = ""
ANSI_CYAN = ""
ANSI_RESET = ""

# Runtime configuration
_TTS_VOICE: str = os.getenv("TTS_VOICE", "man").strip() or "man"
_TTS_LANGUAGE: str = os.getenv("TTS_LANGUAGE", "en-US").strip() or "en-US"
_COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Your Company").strip() or "Your Company"
_TOPIC: str = os.getenv("TOPIC", "your recent experience").strip() or "your recent experience"

_GREETING_WAIT_TIMEOUT_S: int = max(1, min(10, int(os.getenv("GREETING_WAIT_TIMEOUT_SECONDS", "2"))))
_GREETING_MAX_CYCLES: int = max(1, int(os.getenv("GREETING_MAX_CYCLES", "3")))

_DEFAULT_GREETING_KEYWORDS = [
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
    "this is", "speaking", "yes", "how are you", "how can i help", "go ahead"
]
_GREETING_KEYWORDS: List[str] = [kw.strip().lower() for kw in os.getenv(
    "GREETING_KEYWORDS",
    ",".join(_DEFAULT_GREETING_KEYWORDS)
).split(",") if kw.strip()]

# AMD configuration
_ENABLE_AMD: bool = os.getenv("ENABLE_AMD", "true").strip().lower() in {"1", "true", "yes", "on"}
_AMD_MODE: str = os.getenv("AMD_MODE", "Enable").strip() or "Enable"  # or "DetectMessageEnd"
try:
    _AMD_TIMEOUT_S: int = max(3, min(59, int(os.getenv("AMD_TIMEOUT_SECONDS", "20"))))
except ValueError:
    _AMD_TIMEOUT_S = 20

# Recording configuration
_RECORD_CALLS: bool = os.getenv("RECORD_CALLS", "false").strip().lower() in {"1", "true", "yes", "on"}
_RECORDING_CHANNELS: str = os.getenv("RECORDING_CHANNELS", "mono").strip().lower() or "mono"
_RECORDING_STATUS_EVENTS: List[str] = [e.strip() for e in os.getenv("RECORDING_STATUS_EVENTS", "in-progress,completed").split(",") if e.strip()]

# Twilio client (set in main)
_TWILIO_CLIENT: Optional[Client] = None

# Per-call speech transcript and assistant lines
_call_state_lock = threading.Lock()
# CallSid -> {
#   start_ts: float,
#   timer: threading.Timer | None,
#   segments: List[str],  # ordered lines including "Assistant:" and "Callee:"
#   closed: bool
# }
_call_state: Dict[str, Dict[str, Any]] = {}

# Per-call diagnostics for outcome classification
_diag_lock = threading.Lock()
_call_diag: Dict[str, Dict[str, Any]] = {}  # CallSid -> diagnostic fields


def setup_logging() -> None:
    global ANSI_BOLD, ANSI_CYAN, ANSI_RESET
    color_enabled = os.getenv("LOG_COLOR", "1").strip().lower() not in {"0", "false", "no", "off"}
    ANSI_BOLD = "\033[1m" if color_enabled else ""
    ANSI_CYAN = "\033[36m" if color_enabled else ""
    ANSI_RESET = "\033[0m" if color_enabled else ""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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


def _contains_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or not re.search(r"[a-z]", t):
        return False
    for kw in _GREETING_KEYWORDS:
        if kw and kw in t:
            return True
    # Fallback: two or more words counts as a greeting
    return len(re.findall(r"\b\w+\b", t)) >= 2


def _append_line(call_sid: str, role: str, text: str) -> None:
    """
    Append a labeled line to the per-call transcript and log it immediately.
    role: "Assistant" or "Callee"
    """
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


def _ensure_call_state(call_sid: str) -> None:
    with _call_state_lock:
        if call_sid not in _call_state:
            _call_state[call_sid] = {"start_ts": time.time(), "timer": None, "segments": [], "closed": False}
    with _diag_lock:
        if call_sid not in _call_diag:
            _call_diag[call_sid] = {
                "created_ts": time.time(),
                "events": [],                 # list of dicts: {t, event, call_status, answered_by, sip_code, duration}
                "ringing_ts": None,           # first time we saw ringing
                "answered_ts": None,
                "answered_by": None,          # "human", "machine", "machine_start", "machine_end_beep", etc.
                "final_status": None,         # Twilio CallStatus at completed
                "sip_code": None,             # last reported SipResponseCode
                "duration": None,             # CallDuration at completed (string seconds)
                "recording_urls": [],         # list of recording URLs reported by Twilio
                "recording_sids": [],         # list of recording SIDs
            }


def _schedule_forced_hangup(call_sid: str, seconds: int = 60) -> None:
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
    with _diag_lock:
        d = _call_diag.get(call_sid)
        if not d:
            return
        d["events"].append({
            "t": now,
            "event": event,
            "call_status": call_status,
            "answered_by": answered_by,
            "sip_code": sip_code,
            "duration": duration,
        })
        if event == "ringing" and d["ringing_ts"] is None:
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
    """
    Heuristic classification based on status callback sequence, AnsweredBy, and timing.
    """
    with _diag_lock:
        d = _call_diag.get(call_sid, {})

    ans_by = (d.get("answered_by") or "").lower()
    ring_ts = d.get("ringing_ts")
    ans_ts = d.get("answered_ts")
    final_status = (d.get("final_status") or "").lower()
    sip = d.get("sip_code")
    duration_s = None
    try:
        duration_s = int(d.get("duration")) if d.get("duration") is not None else None
    except Exception:
        duration_s = None

    # Human answered
    if ans_by.startswith("human"):
        return "Human answered"

    # Machine/voicemail answered
    if ans_by.startswith("machine"):
        if ans_ts is not None:
            # If we never saw ringing, or answer came very fast, likely direct-to-voicemail/blocked/DND
            if ring_ts is None or (ans_ts - d.get("created_ts", ans_ts)) < 3:
                return "Voicemail or call forwarded immediately (likely DND/blocked-to-voicemail)"
            # If ringing preceded answer for a while, likely no-answer then voicemail
            if ring_ts is not None and (ans_ts - ring_ts) >= 10:
                return "No answer; voicemail after ringing"
        return "Voicemail detected"

    # No 'answered' event at all: use status and SIP code
    # Busy
    if final_status == "busy" or sip in {"486"}:
        return "Busy"

    # Declined/blocked/unwanted indications (carrier- or device-level)
    if final_status == "failed" or sip in {"603", "607", "403"}:
        return "Declined/Blocked (carrier/device rejection)"

    # No answer (no voicemail picked up on network)
    if final_status in {"no-answer", "canceled"}:
        return "No answer"

    # Completed with zero duration (edge)
    if final_status == "completed" and (duration_s == 0):
        return "Completed with zero duration (possible immediate hangup)"

    return "Outcome unknown"


def twiml_response(xml: str) -> Response:
    return Response(xml, status=200, mimetype="text/xml")


@app.route("/voice", methods=["POST"])
def voice_entrypoint() -> Response:
    """
    Initial TwiML for an outbound call.
    1) Start a short <Gather> to detect a human greeting.
    2) Twilio will POST the initial transcription to /greet.
    """
    call_sid = request.form.get("CallSid", "")
    _ensure_call_state(call_sid)
    _schedule_forced_hangup(call_sid, seconds=60)

    # Initial short listen for greeting
    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/greet?cycle=1' "
        f"timeout='{max(1, min(10, _GREETING_WAIT_TIMEOUT_S))}' "
        f"speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
        f"actionOnEmptyResult='true'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/greet", methods=["POST"])
def greet_handler() -> Response:
    """
    Receives the callee's initial greeting (if any). If we detect a human greeting,
    we log it and then play our greeting and prompt. Otherwise, we keep listening
    (up to GREETING_MAX_CYCLES) to avoid triggering on non-human audio.
    """
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    cycle = int(request.args.get("cycle", "1") or "1")

    if speech_text:
        _append_callee_line(call_sid, speech_text)

    if not _contains_greeting(speech_text) and cycle < _GREETING_MAX_CYCLES:
        # Continue short listen loop
        next_cycle = cycle + 1
        xml = (
            "<Response>"
            f"<Gather input='speech' method='POST' action='/greet?cycle={next_cycle}' "
            f"timeout='{max(1, min(10, _GREETING_WAIT_TIMEOUT_S))}' "
            f"speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
            f"actionOnEmptyResult='true'/>"
            "</Response>"
        )
        return twiml_response(xml)

    # Proceed with our greeting and prompt (male voice), then start continuous listen
    greeting_line = f"Hello. Is this {_COMPANY_NAME} calling about {_TOPIC}."
    formatted_prompt = random.choice(ROTATING_PROMPTS).format(company_name=_COMPANY_NAME, topic=_TOPIC)

    # Log exactly what will be spoken to the callee
    _append_assistant_line(call_sid, greeting_line)
    _append_assistant_line(call_sid, formatted_prompt)

    say_voice = _escape_xml(_TTS_VOICE)
    say_lang = _escape_xml(_TTS_LANGUAGE)

    # After the prompt, begin listening. We will re-open <Gather> after each result to continue capturing speech.
    xml = (
        "<Response>"
        f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(greeting_line)}</Say>"
        "<Pause length='1'/>"
        f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(formatted_prompt)}</Say>"
        "<Pause length='1'/>"
        f"<Gather input='speech' method='POST' action='/transcribe?seq=1' "
        f"timeout='60' speechTimeout='auto' language='{say_lang}' actionOnEmptyResult='true'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/transcribe", methods=["POST"])
def transcribe_handler() -> Response:
    """
    Receives the callee's speech in segments. We append each transcription
    and continue listening until the call ends or the 60-second cap is reached
    (enforced by the scheduled forced hangup).
    """
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    seq = int(request.args.get("seq", "1") or "1")

    if speech_text:
        _append_callee_line(call_sid, speech_text)

    # Continue to listen with another Gather. Twilio will break on silence and post again.
    # We keep the loop simple to avoid adding latency.
    next_seq = seq + 1
    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/transcribe?seq={next_seq}' "
        f"timeout='60' speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
        f"actionOnEmptyResult='true'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/status", methods=["POST"])
def status_handler() -> Response:
    """
    Twilio call status callback. We log each event in the lifecycle and classify outcome at completion.
    This route is invoked for events we requested in status_callback_event.
    """
    call_sid = request.form.get("CallSid", "")
    event = request.form.get("StatusCallbackEvent", "")
    call_status = request.form.get("CallStatus", "")
    answered_by = request.form.get("AnsweredBy", "")  # requires machine_detection on the call
    sip_code = request.form.get("SipResponseCode", "")
    call_duration = request.form.get("CallDuration", "")

    # Record event for diagnostics
    if call_sid:
        _record_event(call_sid, event=event, call_status=call_status, answered_by=answered_by or None, sip_code=sip_code or None, duration=call_duration or None)

    # Minimal, privacy-aware status log (avoid echoing phone numbers)
    logging.info(
        "Status callback: event=%s call_status=%s answered_by=%s sip_code=%s duration=%s CallSid=%s",
        event or "<none>", call_status or "<none>", answered_by or "<none>", sip_code or "<none>", call_duration or "<none>", call_sid or "<none>"
    )

    # At completion, print classification and final transcript
    if event == "completed":
        outcome = _classify_outcome(call_sid)
        summary_header = f"{ANSI_BOLD}{ANSI_CYAN}===== Call outcome (CallSid={call_sid}) ====={ANSI_RESET}"
        summary_footer = f"{ANSI_BOLD}{ANSI_CYAN}===== End outcome ====={ANSI_RESET}"
        logging.info("\n%s\nOutcome: %s\nDetails: status=%s answered_by=%s sip=%s duration=%ss\n%s",
                     summary_header, outcome, call_status or "<none>", answered_by or "<none>", sip_code or "<none>", call_duration or "0", summary_footer)

        _cancel_forced_hangup(call_sid)
        _finalize_transcript(call_sid)

        # Cleanup state
        with _call_state_lock:
            _call_state.pop(call_sid, None)
        with _diag_lock:
            _call_diag.pop(call_sid, None)

    return Response("", status=204)


@app.route("/recording-status", methods=["POST"])
def recording_status_handler() -> Response:
    """
    Twilio recording status callback.
    Logs recording SID, status, and URL when available.
    """
    call_sid = request.form.get("CallSid", "")
    rec_sid = request.form.get("RecordingSid", "")
    rec_status = request.form.get("RecordingStatus", "")
    rec_url = request.form.get("RecordingUrl", "")  # Base URL without file extension
    rec_channels = request.form.get("RecordingChannels", "")
    rec_source = request.form.get("RecordingSource", "")

    if call_sid and rec_sid:
        with _diag_lock:
            d = _call_diag.get(call_sid)
            if d is not None:
                if rec_url:
                    d["recording_urls"].append(rec_url)
                d["recording_sids"].append(rec_sid)

    logging.info(
        "Recording callback: CallSid=%s RecordingSid=%s Status=%s Channels=%s Source=%s URL=%s",
        call_sid or "<none>", rec_sid or "<none>", rec_status or "<none>", rec_channels or "<none>", rec_source or "<none>", rec_url or "<none>"
    )

    return Response("", status=204)


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
            raise ValueError(f"Invalid country code in ALLOWED_COUNTRY_CODES: {part}")
        codes.add(part)
    return codes or default


def enforce_country_allowlist(e164_number: str, allowed: Set[str]) -> None:
    if not re.fullmatch(r"\+\d{8,15}", e164_number):
        raise ValueError(f"Not a valid E.164 number: {e164_number}")
    if not any(e164_number.startswith(code) for code in allowed):
        allowed_str = ", ".join(sorted(allowed))
        raise ValueError(
            f"Destination number is not within allowed country codes. Allowed: {allowed_str}. "
            f"Set ALLOWED_COUNTRY_CODES to override."
        )


def start_flask_server(listen_host: str, listen_port: int) -> None:
    app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False)


def ensure_public_base_url() -> Tuple[str, Optional[threading.Thread]]:
    """
    Returns (public_base_url, server_thread).
    Requires PUBLIC_BASE_URL to be set.
    """
    listen_host = os.getenv("LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.getenv("LISTEN_PORT", "5005"))
    public_base_url = os.getenv("PUBLIC_BASE_URL", None)

    # Start the local Flask server in a background thread
    server_thread = threading.Thread(target=start_flask_server, args=(listen_host, listen_port), daemon=True)
    server_thread.start()

    if public_base_url:
        public_base_url = public_base_url.rstrip("/")
        logging.info("Using PUBLIC_BASE_URL: %s", public_base_url)
        return public_base_url, server_thread

    raise RuntimeError("No PUBLIC_BASE_URL provided. Set this to your ngrok public URL so Twilio can reach your server.")


def place_call(client: Client, url: str, from_number: str, to_number: str) -> str:
    """
    Place an outbound call with a status callback for completion and AMD (if enabled).
    Optionally enable Twilio call recording with recording status callbacks.
    Returns the Call SID.
    """
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
        # Synchronous AMD: Twilio will include AnsweredBy in callbacks
        create_kwargs["machine_detection"] = _AMD_MODE
        create_kwargs["machine_detection_timeout"] = _AMD_TIMEOUT_S

    if _RECORD_CALLS:
        # Instruct Twilio to record the call; capture URLs via recording status callbacks.
        # Recording URL received in callback does not include file extension; append ".mp3" or ".wav" when fetching.
        create_kwargs["record"] = True
        create_kwargs["recording_channels"] = "dual" if _RECORDING_CHANNELS == "dual" else "mono"
        create_kwargs["recording_status_callback"] = url.replace("/voice", "/recording-status")
        create_kwargs["recording_status_callback_method"] = "POST"
        # Events may include: "in-progress", "completed"
        create_kwargs["recording_status_callback_event"] = _RECORDING_STATUS_EVENTS or ["completed"]

    call = client.calls.create(**create_kwargs)
    logging.info("Call initiated. SID=%s To=%s From=%s", call.sid, to_number, from_number)
    return call.sid


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    raw_from = os.getenv("FROM_NUMBER")
    raw_to = os.getenv("TO_NUMBER")

    if not all([account_sid, auth_token, raw_from, raw_to]):
        logging.error("Missing required environment variables. Ensure TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, FROM_NUMBER, TO_NUMBER are set.")
        return 2

    try:
        interval = int(os.getenv("CALL_INTERVAL_SECONDS", "60"))
    except ValueError:
        interval = 60
    if interval < 60:
        logging.warning("CALL_INTERVAL_SECONDS=%s is less than 60; adjusting to 60.", interval)
        interval = 60

    try:
        max_calls = int(os.getenv("MAX_CALLS", "0"))
    except ValueError:
        max_calls = 0

    allowed_cc_env = os.getenv("ALLOWED_COUNTRY_CODES", "+1")
    try:
        allowed_cc = parse_allowed_country_codes(allowed_cc_env)
    except ValueError as e:
        logging.error(str(e))
        return 2

    try:
        from_number = normalize_to_e164(raw_from)
        to_number = normalize_to_e164(raw_to)
        enforce_country_allowlist(to_number, allowed_cc)
    except ValueError as e:
        logging.error(str(e))
        return 2

    try:
        public_base_url, server_thread = ensure_public_base_url()
    except Exception as e:
        logging.error("Failed to establish public URL: %s", str(e))
        return 2

    # Twilio client
    global _TWILIO_CLIENT
    http_client = TwilioHttpClient(timeout=30)
    _TWILIO_CLIENT = Client(account_sid, auth_token, http_client=http_client)

    logging.info(
        "Starting call loop. interval=%ss, max_calls=%s (0=infinite). From=%s To=%s, greeting_wait_timeout=%ss, greeting_cycles=%s, Voice=%s, AMD=%s(%s/%ss), Recording=%s(%s)",
        interval, max_calls, from_number, to_number, _GREETING_WAIT_TIMEOUT_S, _GREETING_MAX_CYCLES, _TTS_VOICE,
        "on" if _ENABLE_AMD else "off", _AMD_MODE, _AMD_TIMEOUT_S,
        "on" if _RECORD_CALLS else "off", _RECORDING_CHANNELS
    )

    calls_made = 0
    while not _STOP_REQUESTED:
        if max_calls and calls_made >= max_calls:
            logging.info("Reached MAX_CALLS=%s. Exiting.", max_calls)
            break

        # Prepare the TwiML entrypoint for this call
        msg_url = f"{public_base_url}/voice"

        try:
            sid = place_call(_TWILIO_CLIENT, url=msg_url, from_number=from_number, to_number=to_number)
        except TwilioRestException as e:
            logging.error("Twilio error (status %s, code %s): %s", getattr(e, "status", None), getattr(e, "code", None), str(e))
            return 3
        except Exception as e:
            logging.exception("Unexpected error placing call: %s", str(e))
            return 3

        calls_made += 1

        # Sleep until the next call unless a stop is requested
        slept = 0
        while slept < interval and not _STOP_REQUESTED:
            time.sleep(min(1, interval - slept))
            slept += 1

    logging.info("Stopped. Total calls attempted: %s", calls_made)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
