#!/usr/bin/env python3
"""
Twilio outbound caller (wait-for-callee-first, fast conversational response, two-column live view)

Objectives
- Place an outbound call at a configurable interval.
- Do not speak first; wait until the callee says something, then immediately say the caller message.
- Allow interruption (barge-in) and capture speech with low latency.
- Log exactly what is spoken to the callee (assistant lines) and what the callee says (transcriptions) in a two-column live view during the call.
- Optionally record the call audio via Twilio and log recording URLs when available.
- Keep the call open until the callee ends it or until 60 seconds have elapsed, whichever comes first.

Enhancements for natural, less robotic feel
- The call begins with a listen-only <Gather>. As soon as the callee speaks, we respond with the greeting/prompt.
- Use a barge-in-enabled <Gather> while speaking so the callee can interrupt immediately.
- Support partial-result callbacks to surface interim speech faster in the live view.
- Default AMD disabled to avoid answer latency; can be enabled if desired.

Interactive features
- On startup, prompt whether to use the default TO_NUMBER or enter a different destination.
- Allow overriding the greeting and the message/prompt that is spoken.
- Option to send repeated SMS instead of calls, with the same interval and count settings.

Requirements
- Python 3.8+
- pip install twilio flask
- (optional) pip install python-dotenv  # auto-loads .env

Networking (call mode)
- Twilio must reach a publicly accessible URL to fetch TwiML and to POST transcriptions and status/recording callbacks.
- Provide a public base URL via PUBLIC_BASE_URL.

Environment variables (required unless noted)
- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN
- FROM_NUMBER                  E.164, your Twilio voice/SMS number (e.g., +12095550123)
- TO_NUMBER                    E.164 destination number (e.g., +18315550123)

Scheduling
- CALL_INTERVAL_SECONDS        Default: 60 (minimum enforced)
- MAX_CALLS                    Default: 0 (0 means infinite loop)

Public URL configuration (call mode only)
- PUBLIC_BASE_URL              Example: "https://your-domain.example.com"
- LISTEN_HOST                  Default: "0.0.0.0"
- LISTEN_PORT                  Default: "5005"

Initial listen configuration (callee-first)
- GREETING_WAIT_TIMEOUT_SECONDS Default: 2  (1..10) duration of each short listen cycle
- GREETING_MAX_CYCLES           Default: 30 maximum short-listen cycles before giving up (60s total with default timeout)

Voice and content
- TTS_VOICE                    Default: "man" (consider 'Polly.Matthew' or 'Polly.Joanna' for more natural speech)
- TTS_LANGUAGE                 Default: "en-US"
- COMPANY_NAME                 Default: "Your Company" (used in greeting)
- TOPIC                        Default: "your recent experience" (used in prompt formatting)

Answering Machine Detection (AMD)
- ENABLE_AMD                   Default: "false" (Enable only if you accept added delay before TwiML runs)
- AMD_MODE                     Default: "Enable" (also "DetectMessageEnd" to wait for beep)
- AMD_TIMEOUT_SECONDS          Default: 20 (3..59)

Recording (optional, call mode)
- RECORD_CALLS                 "true"/"false" (default: "false")
- RECORDING_CHANNELS           "mono" or "dual" (default: "mono")
- RECORDING_STATUS_EVENTS      CSV of events (default: "in-progress,completed")

Logging
- LOG_COLOR                    Default: "1" (enable ANSI color/bold for transcription lines)
- During an active call, all stdout logging is suppressed except the two-column transcript.

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
import shutil
import textwrap
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

# Conversation view control
_CONVERSATION_MODE = False  # When True, suppress non-conversation logs and render two-column live view
_PREV_WERKZEUG_DISABLED: Optional[bool] = None
_PREV_APP_LOGGER_DISABLED: Optional[bool] = None

# Runtime configuration
_TTS_VOICE: str = os.getenv("TTS_VOICE", "man").strip() or "man"
_TTS_LANGUAGE: str = os.getenv("TTS_LANGUAGE", "en-US").strip() or "en-US"
_COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Your Company").strip() or "Your Company"
_TOPIC: str = os.getenv("TOPIC", "your recent experience").strip() or "your recent experience"

# Initial listen configuration (callee-first)
_GREETING_WAIT_TIMEOUT_S: int = max(1, min(10, int(os.getenv("GREETING_WAIT_TIMEOUT_SECONDS", "2"))))
# Default to 30 short cycles so total default wait ≈ 60s (with forced hangup safety)
_GREETING_MAX_CYCLES: int = max(1, int(os.getenv("GREETING_MAX_CYCLES", "30")))

# AMD configuration (default disabled to reduce connect latency)
_ENABLE_AMD: bool = os.getenv("ENABLE_AMD", "false").strip().lower() in {"1", "true", "yes", "on"}
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

# Interactive overrides
_RUN_MODE: str = "call"  # "call" or "sms"
_USER_GREETING: Optional[str] = None
_USER_MESSAGE: Optional[str] = None
_TO_NUMBER_OVERRIDE: Optional[str] = None


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


def _term_width() -> int:
    try:
        return shutil.get_terminal_size((120, 20)).columns
    except Exception:
        return 120


def _print_two_column(role: str, text: str) -> None:
    """
    Render a single role/text message as two columns:
    - Left: Callee
    - Right: Assistant
    """
    width = max(40, _term_width())
    separator = " | "
    col_w = max(20, (width - len(separator)) // 2)

    def wrap_lines(s: str) -> List[str]:
        if not s:
            return [""]
        return textwrap.fill(s, width=col_w, drop_whitespace=True, replace_whitespace=False).splitlines() or [""]

    if role == "Callee":
        left_lines = wrap_lines(text)
        right_lines = [""]
    else:  # Assistant
        left_lines = [""]
        right_lines = wrap_lines(text)

    max_lines = max(len(left_lines), len(right_lines))
    for i in range(max_lines):
        left = left_lines[i] if i < len(left_lines) else ""
        right = right_lines[i] if i < len(right_lines) else ""
        print(f"{left:<{col_w}}{separator}{right:<{col_w}}", flush=True)


def _print_conversation_header_once() -> None:
    width = max(40, _term_width())
    separator = " | "
    col_w = max(20, (width - len(separator)) // 2)
    header_left = "Callee"
    header_right = "Assistant"
    print(f"{header_left:<{col_w}}{separator}{header_right:<{col_w}}", flush=True)
    print(f"{'-' * col_w}{separator}{'-' * col_w}", flush=True)


def _enter_conversation_mode() -> None:
    """
    Suppress general logs and switch to two-column view for the active call.
    """
    global _CONVERSATION_MODE, _PREV_WERKZEUG_DISABLED, _PREV_APP_LOGGER_DISABLED
    if _CONVERSATION_MODE:
        return
    _CONVERSATION_MODE = True
    # Suppress Flask/Werkzeug request logs
    try:
        _PREV_WERKZEUG_DISABLED = logging.getLogger("werkzeug").disabled
        logging.getLogger("werkzeug").disabled = True
    except Exception:
        _PREV_WERKZEUG_DISABLED = None
    try:
        _PREV_APP_LOGGER_DISABLED = app.logger.disabled
        app.logger.disabled = True
    except Exception:
        _PREV_APP_LOGGER_DISABLED = None
    # Suppress all logging to stdout during conversation; only use print() for the two-column view
    logging.disable(logging.CRITICAL)
    _print_conversation_header_once()


def _exit_conversation_mode() -> None:
    global _CONVERSATION_MODE, _PREV_WERKZEUG_DISABLED, _PREV_APP_LOGGER_DISABLED
    if not _CONVERSATION_MODE:
        return
    _CONVERSATION_MODE = False
    # Re-enable logging
    logging.disable(logging.NOTSET)
    # Restore Flask/Werkzeug logger states
    try:
        if _PREV_WERKZEUG_DISABLED is not None:
            logging.getLogger("werkzeug").disabled = _PREV_WERKZEUG_DISABLED
    except Exception:
        pass
    try:
        if _PREV_APP_LOGGER_DISABLED is not None:
            app.logger.disabled = _PREV_APP_LOGGER_DISABLED
    except Exception:
        pass


def _append_line(call_sid: str, role: str, text: str) -> None:
    """
    Append a labeled line to the per-call transcript and output it immediately.
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
    if _CONVERSATION_MODE:
        _print_two_column(role, text)
    else:
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
    if not _CONVERSATION_MODE:
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
                "events": [],                 # list of dicts
                "ringing_ts": None,
                "answered_ts": None,
                "answered_by": None,
                "final_status": None,
                "sip_code": None,
                "duration": None,
                "recording_urls": [],
                "recording_sids": [],
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


def _record_event(call_sid: str, call_status: str, answered_by: Optional[str], sip_code: Optional[str], duration: Optional[str]) -> None:
    now = time.time()
    with _diag_lock:
        d = _call_diag.get(call_sid)
        if not d:
            return
        d["events"].append({
            "t": now,
            "event": call_status or "",
            "call_status": call_status,
            "answered_by": answered_by,
            "sip_code": sip_code,
            "duration": duration,
        })
        if call_status == "ringing" and d["ringing_ts"] is None:
            d["ringing_ts"] = now
        if call_status == "in-progress":
            d["answered_ts"] = now
            if answered_by:
                d["answered_by"] = answered_by
        if call_status == "completed":
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
    duration_s = None
    try:
        duration_s = int(d.get("duration")) if d.get("duration") is not None else None
    except Exception:
        duration_s = None

    if ans_by.startswith("human"):
        return "Human answered"

    if ans_by.startswith("machine"):
        if ans_ts is not None:
            if ring_ts is None or (ans_ts - d.get("created_ts", ans_ts)) < 3:
                return "Voicemail or call forwarded immediately (likely DND/blocked-to-voicemail)"
            if ring_ts is not None and (ans_ts - ring_ts) >= 10:
                return "No answer; voicemail after ringing"
        return "Voicemail detected"

    if final_status == "busy" or sip in {"486"}:
        return "Busy"
    if final_status == "failed" or sip in {"603", "607", "403"}:
        return "Declined/Blocked (carrier/device rejection)"
    if final_status in {"no-answer", "canceled"}:
        return "No answer"
    if final_status == "completed" and (duration_s == 0):
        return "Completed with zero duration (possible immediate hangup)"

    return "Outcome unknown"


def twiml_response(xml: str) -> Response:
    return Response(xml, status=200, mimetype="text/xml")


@app.route("/voice", methods=["POST"])
def voice_entrypoint() -> Response:
    """
    Initial TwiML for an outbound call.
    Do not speak first; start with a listen-only Gather and wait for the callee to say something.
    As soon as we get speech, /wait_for_callee will respond with our greeting/prompt.
    """
    call_sid = request.form.get("CallSid", "")
    _ensure_call_state(call_sid)
    _schedule_forced_hangup(call_sid, seconds=60)

    # Enter live conversation view as soon as call is in-progress (status callback), but here we only listen.
    # First short listen cycle
    say_lang = _escape_xml(_TTS_LANGUAGE)
    timeout_s = max(1, min(10, _GREETING_WAIT_TIMEOUT_S))
    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/wait_for_callee?cycle=1' "
        f"timeout='{timeout_s}' speechTimeout='auto' language='{say_lang}' "
        f"actionOnEmptyResult='true' "
        f"partialResultCallback='/transcribe-partial' partialResultCallbackMethod='POST'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/wait_for_callee", methods=["POST"])
def wait_for_callee_handler() -> Response:
    """
    Handle the initial callee speech. Only after we receive their first speech segment
    do we respond with our greeting and prompt (callee-first conversation start).
    If empty, continue short listen cycles up to GREETING_MAX_CYCLES.
    """
    call_sid = request.form.get("CallSid", "")
    speech_text = (request.form.get("SpeechResult") or "").strip()
    cycle = int(request.args.get("cycle", "1") or "1")

    if speech_text:
        # Switch to conversation view and log callee line
        _enter_conversation_mode()
        _append_callee_line(call_sid, speech_text)

        # Build greeting and prompt (allow user overrides)
        say_voice = _escape_xml(_TTS_VOICE)
        say_lang = _escape_xml(_TTS_LANGUAGE)
        greeting_line = (_USER_GREETING.strip() if _USER_GREETING else f"Hello. Is this {_COMPANY_NAME} calling about {_TOPIC}.")
        prompt_line = (_USER_MESSAGE.strip() if (_USER_MESSAGE and _USER_MESSAGE.strip()) else random.choice(ROTATING_PROMPTS).format(company_name=_COMPANY_NAME, topic=_TOPIC))

        # Log what we are about to say (appears in the right column)
        _append_assistant_line(call_sid, greeting_line)
        _append_assistant_line(call_sid, prompt_line)

        # Speak and listen simultaneously; allow interruption (barge-in)
        xml = (
            "<Response>"
            f"<Gather input='speech' method='POST' action='/transcribe?seq=1' "
            f"timeout='60' speechTimeout='auto' language='{say_lang}' "
            f"actionOnEmptyResult='true' bargeIn='true' "
            f"partialResultCallback='/transcribe-partial' partialResultCallbackMethod='POST'>"
            f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(greeting_line)}</Say>"
            f"<Pause length='0.5'/>"
            f"<Say voice='{say_voice}' language='{say_lang}'>{_escape_xml(prompt_line)}</Say>"
            "</Gather>"
            "</Response>"
        )
        return twiml_response(xml)

    # No speech yet: continue short listen cycles (bounded by GREETING_MAX_CYCLES; forced hangup still applies)
    next_cycle = cycle + 1
    if next_cycle > _GREETING_MAX_CYCLES:
        # Give up politely without speaking if no speech ever received
        # Continue to listen a final long window so the callee could still speak; otherwise the call will end by forced hangup.
        say_lang = _escape_xml(_TTS_LANGUAGE)
        xml = (
            "<Response>"
            f"<Gather input='speech' method='POST' action='/wait_for_callee?cycle={next_cycle}' "
            f"timeout='10' speechTimeout='auto' language='{say_lang}' "
            f"actionOnEmptyResult='true' "
            f"partialResultCallback='/transcribe-partial' partialResultCallbackMethod='POST'/>"
            "</Response>"
        )
        return twiml_response(xml)

    say_lang = _escape_xml(_TTS_LANGUAGE)
    timeout_s = max(1, min(10, _GREETING_WAIT_TIMEOUT_S))
    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/wait_for_callee?cycle={next_cycle}' "
        f"timeout='{timeout_s}' speechTimeout='auto' language='{say_lang}' "
        f"actionOnEmptyResult='true' "
        f"partialResultCallback='/transcribe-partial' partialResultCallbackMethod='POST'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/transcribe-partial", methods=["POST"])
def transcribe_partial_handler() -> Response:
    """
    Receives partial/interim speech recognition hypotheses for faster live updates.
    These are not added to the saved transcript; they are only printed in the live two-column view.
    """
    call_sid = request.form.get("CallSid", "")
    text = (request.form.get("UnstableSpeechResult") or request.form.get("SpeechResult") or "").strip()
    if text:
        _print_two_column("Callee", f"{text} …")
    return Response("", status=204)


@app.route("/transcribe", methods=["POST"])
def transcribe_handler() -> Response:
    """
    Receives final callee speech segments.
    After each result, continue listening with a new barge-in-enabled Gather to keep it fluid.
    """
    call_sid = request.form.get("CallSid", "")
    speech_text = request.form.get("SpeechResult", "") or ""
    seq = int(request.args.get("seq", "1") or "1")

    if speech_text:
        _append_callee_line(call_sid, speech_text)

    # Continue listening. Keep barge-in so we are always responsive.
    next_seq = seq + 1
    xml = (
        "<Response>"
        f"<Gather input='speech' method='POST' action='/transcribe?seq={next_seq}' "
        f"timeout='60' speechTimeout='auto' language='{_escape_xml(_TTS_LANGUAGE)}' "
        f"actionOnEmptyResult='true' bargeIn='true' "
        f"partialResultCallback='/transcribe-partial' partialResultCallbackMethod='POST'/>"
        "</Response>"
    )
    return twiml_response(xml)


@app.route("/status", methods=["POST"])
def status_handler() -> Response:
    """
    Twilio call status callback.
    Use CallStatus transitions ('ringing' -> 'in-progress' -> 'completed') for outcome and timing.
    """
    call_sid = request.form.get("CallSid", "")
    call_status = (request.form.get("CallStatus", "") or "").lower()
    answered_by = request.form.get("AnsweredBy", "")
    sip_code = request.form.get("SipResponseCode", "")
    call_duration = request.form.get("CallDuration", "")

    if call_sid:
        _record_event(call_sid, call_status=call_status, answered_by=answered_by or None, sip_code=sip_code or None, duration=call_duration or None)

    # Ensure conversation mode is on as soon as Twilio marks in-progress (quiet the console early)
    if call_status == "in-progress":
        _enter_conversation_mode()

    if call_status == "completed":
        outcome = _classify_outcome(call_sid)
        # Keep stdout quiet if in conversation mode. If not, print a summary.
        if not _CONVERSATION_MODE:
            summary_header = f"{ANSI_BOLD}{ANSI_CYAN}===== Call outcome (CallSid={call_sid}) ====={ANSI_RESET}"
            summary_footer = f"{ANSI_BOLD}{ANSI_CYAN}===== End outcome ====={ANSI_RESET}"
            logging.info("\n%s\nOutcome: %s\nDetails: status=%s answered_by=%s sip=%s duration=%ss\n%s",
                         summary_header, outcome, call_status or "<none>", answered_by or "<none>", sip_code or "<none>", call_duration or "0", summary_footer)

        _cancel_forced_hangup(call_sid)
        _finalize_transcript(call_sid)

        # Restore logging after the call ends
        _exit_conversation_mode()

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
    (This log line is suppressed during active conversation.)
    """
    call_sid = request.form.get("CallSid", "")
    rec_sid = request.form.get("RecordingSid", "")
    rec_status = request.form.get("RecordingStatus", "")
    rec_url = request.form.get("RecordingUrl", "")
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
    Place an outbound call. By default AMD is disabled to reduce answer latency.
    Returns the Call SID.
    """
    create_kwargs: Dict[str, Any] = {
        "to": to_number,
        "from_": from_number,
        "url": url,  # Twilio will request /voice after the call is answered
        "method": "POST",
        "status_callback": url.replace("/voice", "/status"),
        "status_callback_method": "POST",
        # Twilio will send CallStatus transitions automatically (initiated, ringing, in-progress, completed).
    }
    if _ENABLE_AMD:
        # Enabling AMD can delay TwiML execution. Keep disabled for most conversational cases.
        create_kwargs["machine_detection"] = _AMD_MODE
        create_kwargs["machine_detection_timeout"] = _AMD_TIMEOUT_S

    if _RECORD_CALLS:
        create_kwargs["record"] = True
        create_kwargs["recording_channels"] = "dual" if _RECORDING_CHANNELS == "dual" else "mono"
        create_kwargs["recording_status_callback"] = url.replace("/voice", "/recording-status")
        create_kwargs["recording_status_callback_method"] = "POST"
        create_kwargs["recording_status_callback_event"] = _RECORDING_STATUS_EVENTS or ["completed"]

    call = client.calls.create(**create_kwargs)
    logging.info("Call initiated. SID=%s To=%s From=%s", call.sid, to_number, from_number)
    return call.sid


def send_sms(client: Client, from_number: str, to_number: str, body: str) -> str:
    """
    Send an SMS message. Returns the Message SID.
    """
    msg = client.messages.create(to=to_number, from_=from_number, body=body)
    logging.info("SMS sent. SID=%s To=%s From=%s", msg.sid, to_number, from_number)
    return msg.sid


def interactive_setup(default_to: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """
    Interactively obtain:
    - run_mode: "call" or "sms"
    - to_number: E.164 destination (override or use default)
    - user_greeting: optional spoken greeting override (call mode)
    - user_message: spoken prompt override (call mode) or SMS body (sms mode)
    Returns (run_mode, to_number, user_greeting, user_message)
    If stdin is not a TTY, returns defaults without prompting.
    """
    run_mode = "call"
    to_number = default_to
    user_greeting: Optional[str] = None
    user_message: Optional[str] = None

    if not sys.stdin.isatty():
        return run_mode, to_number, user_greeting, user_message

    print("Select run mode:")
    print("  1) Call (default)")
    print("  2) SMS")
    inp = input("Enter 1 for Call or 2 for SMS [1]: ").strip()
    run_mode = "sms" if inp == "2" else "call"

    print(f"Default TO_NUMBER from environment: {default_to or '<not set>'}")
    use_default = "y"
    if default_to:
        use_default = input("Use default TO_NUMBER? [Y/n]: ").strip().lower() or "y"
    if use_default.startswith("n") or not default_to:
        to_number_inp = input("Enter destination number (E.164 like +12095550123, or 10/11 digits): ").strip()
        to_number = to_number_inp or default_to

    if run_mode == "call":
        g = input("Enter a custom greeting to speak (optional, blank to use default): ").strip()
        p = input("Enter a custom message/prompt to speak after greeting (optional, blank to use rotating prompt): ").strip()
        user_greeting = g or None
        user_message = p or None
    else:
        p = input("Enter the SMS message body to send (blank for default): ").strip()
        user_message = p or None

    return run_mode, to_number, user_greeting, user_message


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    raw_from = os.getenv("FROM_NUMBER")
    raw_to = os.getenv("TO_NUMBER")

    if not all([account_sid, auth_token, raw_from]):
        logging.error("Missing required environment variables. Ensure TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, FROM_NUMBER are set.")
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

    # Interactive setup (may override run mode, destination number, and messages)
    default_to_for_prompt = raw_to or ""
    run_mode, to_number_input, user_greeting, user_message = interactive_setup(default_to_for_prompt)

    # Normalize numbers
    try:
        from_number = normalize_to_e164(raw_from)
        to_number = normalize_to_e164(to_number_input or raw_to or "")
        enforce_country_allowlist(to_number, allowed_cc)
    except ValueError as e:
        logging.error(str(e))
        return 2

    # Expose interactive choices to globals
    global _RUN_MODE, _USER_GREETING, _USER_MESSAGE, _TO_NUMBER_OVERRIDE
    _RUN_MODE = run_mode
    _USER_GREETING = user_greeting
    _USER_MESSAGE = user_message
    _TO_NUMBER_OVERRIDE = to_number

    # Twilio client
    global _TWILIO_CLIENT
    http_client = TwilioHttpClient(timeout=30)
    _TWILIO_CLIENT = Client(account_sid, auth_token, http_client=http_client)

    # If SMS mode, do not start Flask or require public URL
    if _RUN_MODE == "sms":
        sms_body = _USER_MESSAGE or f"Hello from {_COMPANY_NAME} about {_TOPIC}."
        logging.info(
            "Starting SMS loop. interval=%ss, max_messages=%s (0=infinite). From=%s To=%s",
            interval, max_calls, from_number, to_number
        )
        sent = 0
        while not _STOP_REQUESTED:
            if max_calls and sent >= max_calls:
                logging.info("Reached MAX_CALLS=%s. Exiting.", max_calls)
                break
            try:
                send_sms(_TWILIO_CLIENT, from_number=from_number, to_number=to_number, body=sms_body)
            except TwilioRestException as e:
                logging.error("Twilio SMS error (status %s, code %s): %s", getattr(e, "status", None), getattr(e, "code", None), str(e))
                return 3
            except Exception as e:
                logging.exception("Unexpected error sending SMS: %s", str(e))
                return 3

            sent += 1
            slept = 0
            while slept < interval and not _STOP_REQUESTED:
                time.sleep(min(1, interval - slept))
                slept += 1

        logging.info("Stopped. Total SMS messages attempted: %s", sent)
        return 0

    # Call mode requires a public URL (ngrok or similar)
    try:
        public_base_url, _ = ensure_public_base_url()
    except Exception as e:
        logging.error("Failed to establish public URL: %s", str(e))
        return 2

    logging.info(
        "Starting call loop. interval=%ss, max_calls=%s (0=infinite). From=%s To=%s, Voice=%s, AMD=%s(%s/%ss), Recording=%s(%s), initial_listen=%ss x %s cycles",
        interval, max_calls, from_number, to_number, _TTS_VOICE,
        "on" if _ENABLE_AMD else "off", _AMD_MODE, _AMD_TIMEOUT_S,
        "on" if _RECORD_CALLS else "off", _RECORDING_CHANNELS,
        _GREETING_WAIT_TIMEOUT_S, _GREETING_MAX_CYCLES
    )

    calls_made = 0
    while not _STOP_REQUESTED:
        if max_calls and calls_made >= max_calls:
            logging.info("Reached MAX_CALLS=%s. Exiting.", max_calls)
            break

        # Prepare the TwiML entrypoint for this call
        msg_url = f"{public_base_url}/voice"

        try:
            _ = place_call(_TWILIO_CLIENT, url=msg_url, from_number=from_number, to_number=to_number)
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
