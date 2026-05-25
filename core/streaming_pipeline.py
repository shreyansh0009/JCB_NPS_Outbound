"""
StreamingPipeline — Asterisk AudioSocket + Deepgram, multilingual.

Audio format: slin16 PCM (signed 16-bit LE, 8 kHz, mono).

Barge-In Architecture (AudioSocket — no AEC)
---------------------------------------------
AudioSocket has NO echo cancellation. Our TTS audio echoes back through
the caller's phone mic → Asterisk → us. This makes barge-in detection
non-trivial.

Strategy:
  1. While agent speaks: audio is NOT sent to Deepgram (echo prevention).
     Instead, each frame goes into a ring buffer (last 500ms) and its RMS
     energy is measured.
  2. Adaptive energy threshold: we track the average echo energy during TTS
     playback. Real user speech (talking over the agent) is significantly
     louder than echo. Threshold = max(floor, echo_baseline × 2.5).
  3. When energy exceeds threshold for 5+ consecutive frames (100ms), we
     confirm barge-in: cancel TTS, flush the ring buffer to Deepgram
     (so it gets the START of what the user said), and switch to listening.
  4. While agent is processing (LLM thinking, no TTS playing): audio flows
     normally to Deepgram. Transcript-based barge-in is active.
  5. Post-speech cooldown: 400ms after TTS ends, short transcripts are
     ignored (echo tail protection).

Multilingual Deepgram hot-swap:
  When user switches language, we swap the Deepgram connection. During
  the swap (~300-600ms), audio is buffered in _ConnHolder and flushed
  to the new connection once ready.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

from core.orchestrator import Orchestrator
from core.session import CallSession
from core.latency_logger import get_latency_logger
from core.indian_numbers import parse_indian_mobile, count_parsed_digits
from providers.stt.deepgram import DeepgramSTT, DeepgramConnection
from providers.tts.base import BaseTTS
from core.caller_pace_profiler import CallerPaceProfiler
from providers.llm.base import BaseLLM
from providers.vad.vad import get_vad

logger = logging.getLogger(__name__)
_lat = get_latency_logger()  # module-level singleton — no overhead per call

# ── AudioSocket protocol constants ──────────────────────────────────────────
AS_TYPE_UUID = 0x00
AS_TYPE_SLIN = 0x10
AS_TYPE_HANGUP = 0xFF

_AS_FRAME_BYTES = 320    # 20ms of slin16 at 8kHz (160 samples × 2 bytes)
_FRAME_MS = 20           # ms per frame
_SILENCE_FRAME = b'\x00' * _AS_FRAME_BYTES

# Silence padding around each TTS turn
_LEAD_SILENCE_FRAMES = 3   # 200ms — primes Asterisk jitter buffer to prevent first-word clipping
_TRAIL_SILENCE_FRAMES = 15   # 300ms — lets last word fully play out

# TTS playback smoothing.
# Remote TTS providers often deliver chunks in bursts, which can sound like
# "network breakage" if we forward frames to AudioSocket immediately. Keep a
# tiny local buffer so short upstream gaps do not become audible dropouts.
_TTS_STARTUP_PREBUFFER_FRAMES = 6  # 300ms before first voiced audio (absorbs network jitter, ultra-low latency)
_TTS_STEADY_RESERVE_FRAMES = 0     # keep ~160ms local cushion during playback
_TTS_BATCH_MAX_CHARS = 150          # flush to TTS quickly
_TTS_BATCH_MAX_SENTENCES = 2        # stream sentence-by-sentence for lowest latency
_TTS_ONE_SHOT_MAX_CHARS = 40        # one-shot only for micro-phrases ("okay", "hmm"); stream everything else

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
_CONTROL_RE = re.compile(r'\[(?:HANDOFF|END_CALL|MCP|TOOL):[^\]]*\]|\[END_CALL\]')
_INLINE_TAG_RE = re.compile(r"\[[^\]]+:[^\]]+\]")
_DEBOUNCE_MS = 300

_HANDOFF_SPEECH_HINTS: tuple[str, ...] = (
    "transfer", "transferring", "connect", "connecting", "routing", "handoff",
    "route you", "next officer", "next executive", "next agent", "specialist",
    "अगले अधिकारी", "अगले एजेंट", "ट्रांसफर", "कनेक्ट", "रूट", "हैंडऑफ",
    "जोड़ रही", "जोड रही", "पास भेज रही", "पास भेज रहा",
)

# Urdu/Persian farewell words that must never be spoken — replaced before TTS.
# Regex patterns are pre-compiled for speed; substitutions are applied inside
# _sanitize_spoken_text which is called on every text chunk sent to TTS.
_URDU_BANNED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\bअलविदा\b', re.IGNORECASE), "आपका दिन शुभ हो"),
    (re.compile(r'\bAlvida\b', re.IGNORECASE), "have a great day"),
    (re.compile(r'\bखुदा\s*हाफ़?िज़?\b', re.IGNORECASE), "आपका दिन शुभ हो"),
    (re.compile(r'\bKhuda\s*Hafiz\b', re.IGNORECASE), "have a great day"),
    (re.compile(r'\bमेहरबानी\b', re.IGNORECASE), "शुक्रिया"),
    (re.compile(r'\bMeherbani\b', re.IGNORECASE), "thank you"),
]

# ── Digit-word normaliser ────────────────────────────────────────────────────
# When the LLM confirms a mobile number it spells it out as
# "nine eight seven six five four three two one zero".
# TTS reads each word as a separate token with a long pause → very slow.
# We convert runs of 5+ digit words into paired numeric groups:
#   "nine eight seven six five four three two one zero" → "98 76 54 32 10"
# TTS then reads these as two-digit numbers (ninety-eight, seventy-six…)
# which is ~2× faster and still perfectly intelligible.

_DIGIT_WORD_MAP: dict[str, str] = {
    'zero': '0', 'oh': '0',
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}

_DIGIT_WORD_SEQ_RE = re.compile(
    r'\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)'
    r'(?:[ \t]+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)){4,}\b',
    re.IGNORECASE,
)


def _normalize_digit_words(text: str) -> str:
    """
    Replace spelled-out digit runs with paired numeric groups.
    Only fires on 5+ consecutive digit words — won't affect normal sentences
    that happen to mention 'one' or 'two'.
    """
    def _to_pairs(m: re.Match) -> str:
        words = m.group(0).lower().split()
        digits = ''.join(_DIGIT_WORD_MAP.get(w, '') for w in words)
        return ' '.join(digits[i:i+2] for i in range(0, len(digits), 2))
    return _DIGIT_WORD_SEQ_RE.sub(_to_pairs, text)


_HINDI_DIGIT_MAP: dict[int, str] = {
    0: "शून्य", 1: "एक", 2: "दो", 3: "तीन", 4: "चार",
    5: "पाँच", 6: "छह", 7: "सात", 8: "आठ", 9: "नौ", 10: "दस",
}

# English number words the LLM sometimes outputs instead of digits.
# Compiled once; applied in the Hindi TTS path so TTS never speaks them in English.
_HINDI_NUMBER_WORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bzero\b",  re.IGNORECASE), "शून्य"),
    (re.compile(r"\bone\b",   re.IGNORECASE), "एक"),
    (re.compile(r"\btwo\b",   re.IGNORECASE), "दो"),
    (re.compile(r"\bthree\b", re.IGNORECASE), "तीन"),
    (re.compile(r"\bfour\b",  re.IGNORECASE), "चार"),
    (re.compile(r"\bfive\b",  re.IGNORECASE), "पाँच"),
    (re.compile(r"\bsix\b",   re.IGNORECASE), "छह"),
    (re.compile(r"\bseven\b", re.IGNORECASE), "सात"),
    (re.compile(r"\beight\b", re.IGNORECASE), "आठ"),
    (re.compile(r"\bnine\b",  re.IGNORECASE), "नौ"),
    (re.compile(r"\bten\b",   re.IGNORECASE), "दस"),
]

def _replace_digits_hindi(text: str) -> str:
    """
    Replace standalone digit tokens 0–10 AND English number words with Hindi
    words so TTS never reads them in English during a Hindi conversation.
    Numbers > 10 (phone numbers, years, etc.) are left untouched because each
    of their digits is adjacent to another digit and never matches the pattern.
    Examples:
      "7"            → "सात"
      "ten"          → "दस"
      "10 में से 7"  → "दस में से सात"
      "9876543210"   → unchanged  (each digit has a neighbour)
    """
    def _sub(m: re.Match) -> str:
        return _HINDI_DIGIT_MAP.get(int(m.group()), m.group())
    # Match "10" before single digits to avoid "10" → "एकशून्य"
    result = re.sub(r"(?<!\d)(10|[0-9])(?!\d)", _sub, text)
    # Also catch English number words (LLM sometimes outputs "ten" instead of "10")
    for pattern, hindi_word in _HINDI_NUMBER_WORDS:
        result = pattern.sub(hindi_word, result)
    return result


def _sanitize_spoken_text(text: str) -> str:
    """
    Strip inline control tags and any transfer/handoff bridge phrases so they
    are never spoken by TTS. Also replaces banned Urdu/Persian words with
    approved Hindi equivalents before audio is generated.
    """
    clean = _CONTROL_RE.sub("", text or "")
    clean = _INLINE_TAG_RE.sub("", clean)
    # Replace banned Urdu words before the text reaches TTS
    for pattern, replacement in _URDU_BANNED:
        clean = pattern.sub(replacement, clean)
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", clean) if p.strip()]
    kept: list[str] = []
    for part in parts:
        low = part.lower()
        if any(h in low or h in part for h in _HANDOFF_SPEECH_HINTS):
            continue
        kept.append(part)
    return " ".join(kept).strip()


def _is_repeat_previous_step_request(text: str) -> bool:
    norm = " ".join((text or "").split()).strip()
    if not norm:
        return False
    low = norm.lower()
    repeat_hit = any(h in low or h in norm for h in _REPEAT_STEP_HINTS)
    audio_issue_hit = any(h in low or h in norm for h in _AUDIO_ISSUE_HINTS)
    return repeat_hit or audio_issue_hit


def _last_assistant_step(session: CallSession) -> str:
    """
    Return the latest assistant line suitable for replay.
    Skip silence-reminder/end texts so we replay the real workflow step.
    """
    no_response_lines = set(_NO_RESPONSE_REMINDER_TEXT.values()) | set(_NO_RESPONSE_END_TEXT.values())
    repeat_prefix_markers = (
        "पिछला स्टेप", "ముందు చెప్పిన step", "मागचा step", "முந்தைய step", "previous step",
    )
    for msg in reversed(session.history):
        if msg.get("role") != "assistant":
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if content in no_response_lines:
            continue
        if any(marker in content.lower() for marker in (m.lower() for m in repeat_prefix_markers)):
            continue
        clean = _sanitize_spoken_text(content)
        if clean:
            return clean
    return ""


def _repeat_prefix(lang: str) -> str:
    if lang == "hi":
        return "ज़रूर, मैं पिछला स्टेप दोबारा बोल रही हूँ।"
    if lang == "te":
        return "సరే, నేను ముందు చెప్పిన step ని మళ్లీ చెబుతున్నాను."
    if lang == "mr":
        return "नक्की, मी मागचा step पुन्हा सांगते."
    if lang == "ta":
        return "சரி, நான் முந்தைய step-ஐ மீண்டும் சொல்கிறேன்."
    return "Sure, I will repeat the previous step."


# ── Barge-In Engine constants ───────────────────────────────────────────────
# Tuned for real outbound telephony (slin16 via AudioSocket through carrier).
# Carrier AGC + compression keeps caller-side RMS lower than office-mic levels,
# so floors that work in the lab are too strict on real calls. All values are
# env-overridable so they can be retuned on a live call without redeploy.
_BARGE_RING_SIZE = 25          # 25 frames × 20ms = 500ms audio history

_BARGE_CONFIRM_FRAMES = int(os.getenv("BARGE_CONFIRM_FRAMES", "6"))
                                # 6 × 20ms = 120ms sustained speech to confirm.
                                # Short enough for "haan/nahi" interjections; still
                                # rejects single-impulse noise (which is <40ms).
_BARGE_MIN_ENERGY = float(os.getenv("BARGE_MIN_ENERGY", "1500"))
                                # Phone-line slin16 RMS: 200-800 normal, 1000+ shouting.
                                # Old 1500 floor required shouting to interrupt.
_BARGE_ECHO_MULTIPLIER = float(os.getenv("BARGE_ECHO_MULTIPLIER", "4.0"))
                                # Standard half-duplex rule: speech must be 2× the
                                # rolling echo baseline. Old 4× was unreachable.

_BARGE_AMBIENT_MULTIPLIER = float(os.getenv("BARGE_AMBIENT_MULTIPLIER", "3.0"))

_BARGE_VAD_THRESHOLD = float(os.getenv("BARGE_VAD_THRESHOLD", "0.50"))


_ECHO_WINDOW = 50              # frames averaged for echo baseline (~1s)

_BARGE_HOLD_FRAMES = int(os.getenv("BARGE_HOLD_FRAMES", "3"))
                                # 100ms hysteresis — slightly more grace at the lower
                                # energy floor so a quiet syllable mid-word doesn't
                                # reset the hot-frame counter.

# _BARGE_ZCR_MIN = float(os.getenv("BARGE_ZCR_MIN", "1.2"))
                                # 8kHz carrier-bandlimited voiced speech can sit at
                                # 1.5-2.0 crossings/ms; 2.0 was clipping real speech
                                # on low-pitch male voices through landlines.

_POST_SPEECH_COOLDOWN_S = float(os.getenv("POST_SPEECH_COOLDOWN_S", "0.5"))
                                # Faster turn handoff post-TTS now that barge-in
                                # gates are correct.
_COOLDOWN_MIN_CHARS = int(os.getenv("COOLDOWN_MIN_CHARS", "10"))  # was 10 — allow short valid answers ("7", "हाँ", "yes")

# Per-frame diagnostic log (set BARGE_DEBUG=1 to enable). Logs every 20ms during
# TTS playback so you can grep `BARGE` in call logs and see exactly which gate
# is rejecting the would-be barge-in.
_BARGE_DEBUG = os.getenv("BARGE_DEBUG", "0") == "1"

# Transcript quality / noisy-line handling
_MIN_COMMIT_CHARS = 1           # allow single chars but let cooldown filter short noise
_NO_RESPONSE_REMINDER_S = float(os.getenv("NO_RESPONSE_REMINDER_S", "15"))   # callers often think before answering — 6s was too aggressive
_NO_RESPONSE_AUTO_END_S = float(os.getenv("NO_RESPONSE_AUTO_END_S", "45"))   # graceful wrap only after long inactivity

_NO_RESPONSE_REMINDER_TEXT: dict[str, str] = {
    "hi": "अगर आप कॉल पर हैं तो प्लीज़ answer दीजिए?",
    "en": "If you are on the call, please answer?",
    "te": "మీరు కాల్‌లో ఉన్నట్లయితే, దయచేసి మాట్లాడండి.",
    "mr": "जर आपण कॉलवर असाल, कृपया उत्तर द्या.",
    "ta": "நீங்கள் கால்-ல் இருந்தால், தயவுசெய்து பதிலளிக்கவும்.",
}

_NO_RESPONSE_END_TEXT: dict[str, str] = {
    "hi": "आपकी तरफ से जवाब नहीं मिला, कॉल समाप्त की जा रही है। धन्यवाद।",
    "en": "No response received from your side, ending the call now. Thank you.",
    "te": "మీ వైపు నుండి స్పందన రాలేదు, కాల్‌ను ఇప్పుడు ముగిస్తున్నాము. ధన్యవాదాలు.",
    "mr": "तुमच्या बाजूने प्रतिसाद मिळाला नाही, कॉल आता समाप्त करत आहोत. धन्यवाद.",
    "ta": "உங்கள் பக்கத்தில் பதில் வரவில்லை, கால் இப்போது முடிக்கப்படுகிறது. நன்றி.",
}

_REPEAT_STEP_HINTS: tuple[str, ...] = (
    "repeat", "again", "say again", "once more",
    "फिर से", "दोबारा", "dubara", "dobara", "fir se",
    "మళ్లీ", "malli", "malli cheppu",
    "पुन्हा",
    "மீண்டும்", "tirumba", "thirumba",
)

_AUDIO_ISSUE_HINTS: tuple[str, ...] = (
    "voice cut", "audio cut", "not audible", "can't hear", "cannot hear",
    "aawaz cut", "awaz cut", "sunai nahi", "sunayi nahi", "awaaz nahi",
    "आवाज़ कट", "आवाज कट", "सुनाई नहीं", "आवाज़ नहीं",
    "వాయిస్ కట్", "వినిపించలేదు",
    "आवाज कट", "ऐकू नाही", "आवाज ऐकू",
    "சத்தம் கட்", "கேட்கவில்லை",
)

# ── Utterance commit buffer ──────────────────────────────────────────────────
# Deepgram can mark short pauses inside names / mobile numbers as utterance
# boundaries.  If we react too fast, the agent starts speaking while the user
# is still dictating digits.  Hold longer before committing a turn so users can
# finish saying the entire number without being interrupted.
#
# Cost: +650ms to response latency.
# Benefit: much safer capture of names/mobile numbers and far fewer mid-user
# interruptions on natural pauses between digit groups.
_UTTERANCE_HOLD_MS = 600

# Extended hold while caller is dictating digits (mobile number). Exits early
# on 10 complete digits, or falls back to commit on timeout if the caller
# never finishes. Natural inter-group pauses observed up to ~2.1s in real
# calls — 3500ms gives a safe margin while still ending silent holds in
# under 4s for abandoned dictations.
_DIGIT_HOLD_MS = 3500

# Extended hold while caller is dictating a service address. Addresses are
# multi-part ("Flat 107, Aravali homes, Gandhi path, Vaishali Nagar, Jaipur")
# with long natural pauses between parts. We wait longer here than digits
# because there's no hard length check to short-circuit on.
_ADDRESS_HOLD_MS = 5000

# Phrases in the caller's own utterance that strongly indicate the start of an
# address dictation. When the service agent hasn't captured an address yet and
# the caller's utterance contains one of these, we treat the turn as address
# dictation — latch `expecting_address` and extend the hold so multi-part
# addresses with long inter-group pauses are captured in one commit.
_ADDRESS_START_MARKERS = (
    "मेरा पता", "मेरा पूरा पता", "हमारा पता", "हमारा पूरा पता",
    "पूरा पता", "पूरा address", "पता ये है", "पता यह है",
    "मेरा घर", "मेरा मकान", "घर का पता", "मकान नंबर",
    "रहता हूँ", "रहती हूँ", "रहते हैं",
    "my address", "my full address", "my home address", "my house address",
    "address is", "i live at", "i live in", "i stay at",
    "flat no", "flat number", "house no", "house number",
    "plot no", "plot number", "door no", "door number",
)


def _looks_like_address_start(text: str) -> bool:
    low = text.lower()
    return any(marker.lower() in low for marker in _ADDRESS_START_MARKERS)


def _rms_energy(pcm: bytes) -> float:
    """RMS energy of a slin16 PCM frame (signed 16-bit LE)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f'<{n}h', pcm)
    return (sum(s * s for s in samples) / n) ** 0.5


# def _zcr_per_ms(pcm: bytes) -> float:
#     """Zero-crossing rate per millisecond of a slin16 PCM frame.

#     Voiced human speech: ~5–20 crossings/ms.
#     Impulse noise (hammer, bang): near 0 (one large spike then silent).
#     Unvoiced fricatives (/s/, /f/): 20–50 but low energy — caught by energy gate.
#     """
#     n = len(pcm) // 2
#     if n < 2:
#         return 0.0
#     samples = struct.unpack(f'<{n}h', pcm)
#     crossings = sum(
#         1 for i in range(1, n)
#         if (samples[i] >= 0) != (samples[i - 1] >= 0)
#     )
#     return crossings / _FRAME_MS  # crossings per millisecond


def _drain(queue: asyncio.Queue) -> None:
    """Drop queued outbound audio frames without blocking."""
    try:
        while True:
            queue.get_nowait()
    except asyncio.QueueEmpty:
        return


_DG_LANG: dict[str, str] = {
    "hi": "hi", "bn": "bn", "te": "te", "mr": "mr", "ta": "ta",
    "gu": "gu", "kn": "kn", "pa": "pa", "ml": "ml", "or": "or",
    "en": "en-IN",
}

_LANG_INSTRUCTIONS: dict[str, str] = {
    "hi": "You MUST respond only in Hindi (हिंदी). A single English word or greeting like 'hello' or 'okay' is normal Hinglish usage — it does NOT change this. The system (not you) decides when to switch language.",
    "bn": "You MUST respond only in Bengali (বাংলা). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "te": "You MUST respond only in Telugu (తెలుగు). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "mr": "You MUST respond only in Marathi (मराठी). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "ta": "You MUST respond only in Tamil (தமிழ்). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "gu": "You MUST respond only in Gujarati (ગુજરાતી). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "kn": "You MUST respond only in Kannada (ಕನ್ನಡ). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "pa": "You MUST respond only in Punjabi (ਪੰਜਾਬੀ). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "ml": "You MUST respond only in Malayalam (മലയാളം). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "or": "You MUST respond only in Odia (ଓଡ଼ିଆ). A single English word or greeting does NOT change this. The system (not you) decides when to switch language.",
    "en": "You MUST respond only in English.",
}


# ── AudioSocket frame parser ────────────────────────────────────────────────

class _FrameParser:
    """Buffers incoming TCP bytes and extracts complete AudioSocket frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Feed raw TCP bytes, returns list of (type, payload) tuples."""
        self._buf.extend(data)
        frames = []
        while len(self._buf) >= 3:
            frame_type = self._buf[0]
            length = struct.unpack("!H", self._buf[1:3])[0]
            if len(self._buf) < 3 + length:
                break  # incomplete frame, wait for more data
            payload = bytes(self._buf[3:3 + length])
            self._buf = self._buf[3 + length:]
            frames.append((frame_type, payload))
        return frames


def _make_audio_frame(pcm: bytes) -> bytes:
    """Wrap PCM payload in an AudioSocket SLIN frame."""
    header = struct.pack("!BH", AS_TYPE_SLIN, len(pcm))
    return header + pcm


@dataclass
class _TurnState:
    """
    Tracks the state of the current conversation turn.

    State machine:
      IDLE       (processing=F, speaking=F) — mic open, audio → Deepgram
      PROCESSING (processing=T, speaking=F) — LLM thinking, mic still open
      SPEAKING   (processing=T, speaking=T) — TTS playing, mic gated, energy detection active

    Transitions:
      IDLE → PROCESSING      : transcript received, _handle_turn starts
      PROCESSING → SPEAKING  : _speak() begins TTS output
      SPEAKING → IDLE        : TTS finishes normally (+ cooldown)
      SPEAKING → IDLE        : barge-in detected → cancel turn
      PROCESSING → IDLE      : barge-in (transcript during LLM thinking)
    """
    last_text: str = ""
    last_time: float = 0.0
    processing: bool = False
    speaking: bool = False
    call_ended: bool = False
    end_call_requested: bool = False  # set when agent signals end_call; blocks new turns
    barge_in: asyncio.Event = field(default_factory=asyncio.Event)
    current_turn: asyncio.Task | None = None

    # Persistent TTS session (e.g. CartesiaCallSession) — set by run_audiosocket_call
    # when the TTS backend supports a per-call connection.  None = use self.tts directly.
    call_tts: object = None

    # Barge-in engine state
    _ring_buffer: deque = field(default_factory=lambda: deque(maxlen=_BARGE_RING_SIZE))
    _hot_frames: int = 0        # consecutive speech-candidate frames
    _hold_frames: int = 0       # sub-threshold frames allowed by hysteresis
    _echo_energies: deque = field(default_factory=lambda: deque(maxlen=_ECHO_WINDOW))
    _echo_baseline: float = 0.0
    _ambient_energies: deque = field(default_factory=lambda: deque(maxlen=150))
    _speech_ended_at: float = 0.0
    _spawned_tasks: set = field(default_factory=set)

    # Utterance commit buffer state
    _pending_text: str = ""                  # accumulated transcript fragments
    _hold_task: asyncio.Task | None = None   # fires after _UTTERANCE_HOLD_MS silence
    _last_committed_text: str = ""           # most recent committed turn — used for
                                             # digit-continuation detection if a later
                                             # fragment arrives past the hold window
    _no_response_task: asyncio.Task | None = None  # 5s post-speech nudge timer

    def track_task(self, task: asyncio.Task) -> None:
        """Register a fire-and-forget task so it can be cancelled on call end."""
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)

        

    def cancel_all_tasks(self) -> None:
        """Cancel all tracked fire-and-forget tasks."""
        for task in list(self._spawned_tasks):
            if not task.done():
                task.cancel()

    def reset_barge_engine(self):
        """Reset barge-in engine state at the start of each TTS utterance."""
        self._ring_buffer.clear()
        self._hot_frames = 0
        self._hold_frames = 0
        self._echo_energies.clear()
        self._echo_baseline = 0.0


class _ConnHolder:
    """
    Wraps the active Deepgram connection with audio buffering during swaps.

    States:
      LIVE     — audio goes straight to _conn
      SWAPPING — audio is buffered in _buffer list
      (after swap) — buffer is flushed to new conn, back to LIVE
    """

    def __init__(self, conn: DeepgramConnection):
        self._conn = conn
        self._swapping = False
        self._buffer: list[bytes] = []
        self._lock = asyncio.Lock()

    async def send_audio(self, data: bytes) -> None:
        async with self._lock:
            if self._swapping:
                self._buffer.append(data)
            else:
                await self._conn.send_audio(data)

    async def begin_swap(self) -> None:
        """Call before opening new WS — starts buffering immediately."""
        async with self._lock:
            self._swapping = True
            self._buffer.clear()

    async def finish_swap(self, new_conn: DeepgramConnection) -> None:
        """
        Atomically replace connection and flush buffered audio to new conn.
        Called after new WS is open and ready.
        """
        async with self._lock:
            old_conn = self._conn
            self._conn = new_conn
            self._swapping = False
            buffered = list(self._buffer)
            self._buffer.clear()

        # Flush buffered audio to new connection
        for chunk in buffered:
            await new_conn.send_audio(chunk)

        # Close old connection outside lock
        await asyncio.sleep(0.1)
        try:
            await old_conn.close()
        except Exception:
            pass


class StreamingPipeline:

    def __init__(
        self,
        stt: DeepgramSTT,
        tts: BaseTTS,
        llm: BaseLLM,
        orchestrator: Orchestrator,
        welcome_message: str = "",
        call_registry: dict | None = None,  # deprecated; use core.outbound_call_store
        audio_out_max_frames: int = 200,
        idle_timeout_s: float = 60.0,
    ):
        self.stt = stt
        self.tts = tts
        self.orchestrator = orchestrator
        self.welcome_message = welcome_message
        self._swap_lock = asyncio.Lock()
        # Legacy: kept for backwards compat. Lead context is now read async via
        # core.outbound_call_store (which transparently uses Redis or in-memory).
        self._call_registry = call_registry if call_registry is not None else {}
        self.pace_profiler = CallerPaceProfiler()
        self._vad = get_vad(use_silero=False, threshold=_BARGE_VAD_THRESHOLD)

        # Bounded audio queue: TTS producer blocks when Asterisk drains slow.
        # 200 frames @ 20ms = 4s of buffered audio (safe ceiling without OOM).
        self._audio_out_max_frames = max(50, int(audio_out_max_frames))
        # Idle timeout: if no AudioSocket frames arrive for this long, the call
        # is considered orphaned (Asterisk crashed, network partition, etc.) and
        # the socket is force-closed so the CallGate slot frees.
        self._idle_timeout_s = max(10.0, float(idle_timeout_s))

    # ── Entry point — AudioSocket TCP ────────────────────────────────────────

    async def run_audiosocket_call(
        self,
        call_sid: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a call over Asterisk AudioSocket TCP connection."""
        audio_out: asyncio.Queue = asyncio.Queue(maxsize=self._audio_out_max_frames)
        state = _TurnState()

        # ── Seed caller number from Asterisk register-call metadata ──────────
        # Asterisk may generate a different UUID for AudioSocket than the one
        # passed in the dialplan, so if exact match fails, the store falls back
        # to the most recent registration within a 10-second window.
        from core import outbound_call_store as _store_mod
        try:
            call_meta = await _store_mod.pop(call_sid, window_secs=10.0)
        except Exception as e:
            logger.error(f"[{call_sid}] outbound_call_store.pop failed: {e}")
            call_meta = {}
        # Outbound-only project: every call uses the single outbound orchestrator.
        # Per-call welcome comes from the lead context (formatted at originate time).
        orchestrator = self.orchestrator
        resolved_welcome = call_meta.get("welcome_message") or self.welcome_message
        logger.info(
            f"[{call_sid}] Outbound call — squad='{getattr(orchestrator, '_squad_path', 'unknown')}'"
        )

        session = orchestrator.start_session()
        if resolved_welcome:
            session.set("welcome_message", resolved_welcome)
        session.set("direction", "outbound")
        session.set("support_domain", "outbound")

        caller_number = call_meta.get("callerid", "").strip()
        logger.info(f"[{call_sid}] Call registry lookup: callerid={caller_number!r}")
        if caller_number:
            clean = re.sub(r"^\+?91", "", caller_number)
            if re.fullmatch(r"[6-9]\d{9}", clean):
                session.set("caller_number", clean)
                logger.info(f"[{call_sid}] Caller number seeded into session: {clean}")
            else:
                logger.warning(f"[{call_sid}] Caller number '{caller_number}' -> '{clean}' did not match 10-digit pattern")

        # Seed outbound lead context if present (name, mobile, product, issue,
        # call_type, welcome_message, and any extra keys from the campaign row).
        # call_meta keys starting with "_" are internal (e.g. _ts) and skipped.
        for _k, _v in call_meta.items():
            if _k in ("callerid", "did") or _k.startswith("_") or _v in (None, ""):
                continue
            session.set(_k, _v)
        if call_meta.get("mobile") and not session.get("caller_number"):
            # Outbound flow: use the lead's mobile as the caller number
            _mob = re.sub(r"^\+?91", "", str(call_meta["mobile"]))
            if re.fullmatch(r"[6-9]\d{9}", _mob):
                session.set("caller_number", _mob)

        # Default language is always Hindi. The only override is an explicit
        # preferred_language from the lead CSV (e.g. "en", "ta", "te") or a
        # previously stored value (restored Redis session).
        # Content-based auto-detection is disabled — callers routinely mix
        # English words into Hindi speech and a single "hello" must not flip
        # the language. The tracker only switches on an explicit verbal request.
        _init_lang = session.get("preferred_language") or session.get("language") or "hi"

        # Keep metadata + tracker in sync from the first turn.
        session.set_language(_init_lang)
        session.set("language", _init_lang)
        if hasattr(self.tts, "set_language"):
            self.tts.set_language(_init_lang)

        call_start_mono = time.monotonic()

        # ── Call recording ────────────────────────────────────────────────
        from integrations.call_recorder import CallRecorder
        recorder = CallRecorder(call_sid)

        logger.info(f"[{call_sid}] AudioSocket call started")
        await _lat.separator(call_sid, f"CALL STARTED  sid={call_sid}")
        await _lat.event(call_sid, "CALL_STARTED")

        # Mute mic during startup — prevents noise during Asterisk
        # connection setup from triggering false transcripts.
        # Welcome task sets speaking=False when done.
        state.speaking = True

        conn_holder: _ConnHolder | None = None

        async def on_transcript(text: str):
            if conn_holder is not None:
                await self._on_transcript(
                    text,
                    audio_out,
                    session,
                    call_sid,
                    state,
                    conn_holder,
                    orchestrator,
                )

        async def on_speech_start():
            self.pace_profiler.mark_turn_start()

        async def _run_call_inner():
            """Inner coroutine that runs with the persistent TTS WS already open."""
            async with self.stt.connect(
                on_transcript=on_transcript,
                on_speech_start=on_speech_start,
                endpointing_ms=self.pace_profiler.endpointing_ms,
                get_force_emit_timeout=lambda: self.pace_profiler.force_emit_timeout,
            ) as initial_conn:
                conn_holder_ref[0] = _ConnHolder(initial_conn)

                welcome_task = asyncio.create_task(
                    self._play_welcome_audiosocket(audio_out, call_sid, state, session=session),
                    name="welcome",
                )
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(
                            self._recv_audiosocket(reader, conn_holder_ref[0], audio_out, call_sid, state, recorder),
                            name="recv",
                        ),
                        asyncio.create_task(
                            self._send_audiosocket(writer, audio_out, call_sid, state, recorder),
                            name="send",
                        ),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                done_names = [t.get_name() for t in done]
                logger.info(f"[{call_sid}] Call loop finished first on task(s): {done_names}")
                # ── Cleanup: cancel ALL tasks spawned during this call ──
                state.call_ended = True
                welcome_task.cancel()
                state.cancel_all_tasks()  # kill orphaned _handle_turn / _swap_deepgram
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                # Give spawned tasks a moment to finish cancellation
                if state._spawned_tasks:
                    remaining = [t for t in state._spawned_tasks if not t.done()]
                    if remaining:
                        await asyncio.gather(*remaining, return_exceptions=True)

        # We need conn_holder accessible both inside _run_call_inner and in the
        # on_transcript closure.  Use a single-element list as a mutable cell.
        conn_holder_ref: list[_ConnHolder | None] = [None]

        # Patch on_transcript to read from conn_holder_ref
        async def on_transcript(text: str):  # type: ignore[no-redef]
            if conn_holder_ref[0] is not None:
                await self._on_transcript(
                    text,
                    audio_out,
                    session,
                    call_sid,
                    state,
                    conn_holder_ref[0],
                    orchestrator,
                )

        # ── Open per-call TTS session when the backend supports it ────────────
        # BaseTTS.call_session() yields self, so non-persistent backends still
        # work without any special handling.
        try:
            async with self.tts.call_session() as call_session:
                state.call_tts = call_session if call_session is not self.tts else None
                if state.call_tts is not None:
                    logger.info(f"[{call_sid}] Persistent TTS session open")
                    await _lat.event(call_sid, "TTS_WS_OPEN")
                await _run_call_inner()
        except Exception:
            logger.exception(f"[{call_sid}] Persistent TTS session failed — falling back")
            state.call_tts = None
            await _run_call_inner()

        # ── Finalize recording and upload to Cloudinary ─────────────────
        try:
            recorder.finalize()
            from config.settings import Settings as _RecSettings
            _rec_settings = _RecSettings.from_env()
            recording_url = await recorder.upload_to_cloudinary(_rec_settings)
            session.set("recording_url", recording_url)
            recorder.cleanup()
        except Exception:
            logger.exception(f"[{call_sid}] Recording upload failed")
            session.set("recording_url", "")

        # Save transcript before clearing call state
        from core.transcript_logger import save_transcript
        await save_transcript(call_sid, session, call_start_mono)

        _lat.clear_call(call_sid)
        logger.info(f"[{call_sid}] AudioSocket call ended")
        await _lat.separator(call_sid, f"CALL ENDED    sid={call_sid}")

    # ── Hot-swap Deepgram ─────────────────────────────────────────────────

    def _cancel_no_response_timer(self, state: _TurnState) -> None:
        task = state._no_response_task
        if task and not task.done():
            task.cancel()
        state._no_response_task = None

    def _schedule_no_response_timer(
        self,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        # At most one silence-nudge timer at a time.
        self._cancel_no_response_timer(state)

        async def _prompt_after_silence() -> None:
            try:
                await asyncio.sleep(_NO_RESPONSE_REMINDER_S)
                if state.call_ended or state.processing or state.speaking or state.current_turn is not None:
                    return

                lang = (session.current_language or session.get("language") or "en").lower()
                reminder = _NO_RESPONSE_REMINDER_TEXT.get(lang, _NO_RESPONSE_REMINDER_TEXT["en"])
                last_user_ts = state.last_time
                logger.info(f"[{call_sid}] [NO RESPONSE] silence detected — prompting caller")
                state.barge_in.clear()
                await self._speak(reminder, audio_out, call_sid, state, session=session)
                if not state.barge_in.is_set():
                    state._speech_ended_at = time.monotonic()

                # If still no user response after reminder, end the call gracefully
                await asyncio.sleep(_NO_RESPONSE_AUTO_END_S)
                if state.call_ended or state.processing or state.speaking or state.current_turn is not None:
                    return
                if state.last_time > last_user_ts:
                    return
                end_text = _NO_RESPONSE_END_TEXT.get(lang, _NO_RESPONSE_END_TEXT["en"])
                logger.info(
                    f"[{call_sid}] [NO RESPONSE] auto-ending call after {_NO_RESPONSE_AUTO_END_S:.0f}s post-reminder silence"
                )
                state.barge_in.clear()
                await self._speak(end_text, audio_out, call_sid, state, session=session)
                await audio_out.put(None)
            except asyncio.CancelledError:
                return
            finally:
                if state._no_response_task is asyncio.current_task():
                    state._no_response_task = None

        task = asyncio.create_task(_prompt_after_silence(), name="no_response_prompt")
        state._no_response_task = task
        state.track_task(task)

    async def _swap_deepgram(
        self,
        lang_code: str,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        async with self._swap_lock:
            if state.call_ended:
                return

            dg_lang = _DG_LANG.get(lang_code, lang_code)
            logger.info(f"[{call_sid}] Swap starting → {dg_lang}")

            await conn_holder.begin_swap()

            try:
                new_stt = DeepgramSTT(
                    api_key=self.stt._api_key,
                    model=self.stt._model,
                    language=dg_lang,
                    endpointing_ms=self.stt._endpointing_ms,
                )
                url = new_stt._build_url()
                headers = {"Authorization": f"Token {self.stt._api_key}"}
                ws = await websockets.connect(url, additional_headers=headers)

                async def on_transcript(text: str):
                    await self._on_transcript(
                        text,
                        audio_out,
                        session,
                        call_sid,
                        state,
                        conn_holder,
                        orchestrator,
                    )

                async def on_speech_start_swap():
                    pass  # Disabled — same echo issue

                new_conn = DeepgramConnection(ws, on_transcript, on_speech_start_swap)
                new_conn._start_receive()

                await conn_holder.finish_swap(new_conn)
                logger.info(f"[{call_sid}] Swap complete → {dg_lang}")

            except Exception:
                logger.exception(f"[{call_sid}] Swap FAILED for {dg_lang} — resuming old conn")
                async with conn_holder._lock:
                    conn_holder._swapping = False
                    conn_holder._buffer.clear()

    # ── Welcome message ───────────────────────────────────────────────────

    async def _play_welcome_audiosocket(
        self,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        # Per-call override (outbound flow seeds session.welcome_message with
        # a personalized greeting); fall back to the pipeline default.
        welcome = (session.get("welcome_message") if session is not None else None) or self.welcome_message
        if not welcome:
            state.speaking = False
            return

        state.barge_in.clear()
        state.reset_barge_engine()
        self._vad.reset()

        # Save welcome to history BEFORE playback so a barge-in mid-welcome
        # still leaves the LLM aware that the agent already greeted. Otherwise
        # the next turn's prompt looks like a fresh call and the model may
        # restart with another greeting.
        
        if session is not None:
            session.add_message("assistant", welcome)

        try:
            # Outbound calls: Asterisk has already established the channel by the
            # time AudioSocket connects, so no ring tone is needed.
            for _ in range(_LEAD_SILENCE_FRAMES):
                await audio_out.put(_SILENCE_FRAME)
            await self._tts_to_queue(welcome, audio_out, call_sid, state, session=session)
            for _ in range(_TRAIL_SILENCE_FRAMES):
                await audio_out.put(_SILENCE_FRAME)
        except asyncio.CancelledError:
            logger.info(f"[{call_sid}] Welcome cancelled (barge-in)")
        except Exception:
            logger.exception(f"[{call_sid}] Welcome TTS error")
        finally:
            # Wait for audio to physically finish playing before opening mic
            while not audio_out.empty() and not state.barge_in.is_set():
                await asyncio.sleep(0.05)
            state.speaking = False
            state._speech_ended_at = time.monotonic()
            if session is not None and not state.barge_in.is_set() and not state.call_ended:
                self._schedule_no_response_timer(audio_out, session, call_sid, state)
            logger.info(f"[{call_sid}] Welcome done — mic open")

    # ── Receive from AudioSocket ─────────────────────────────────────────

    async def _recv_audiosocket(
        self,
        reader: asyncio.StreamReader,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        recorder=None,
    ) -> None:
        """
        Reads AudioSocket frames from Asterisk.

        Audio routing depends on state:
          SPEAKING  → frames go to ring buffer + energy detector (not Deepgram)
          otherwise → frames go directly to Deepgram

        On barge-in detection:
          1. Signal barge-in (cancel TTS)
          2. Drain audio output queue
          3. Cancel current turn task
          4. Flush ring buffer to Deepgram (user's speech history)
          5. Resume normal audio forwarding
        """
        parser = _FrameParser()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        reader.read(4096), timeout=self._idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{call_sid}] AudioSocket idle for {self._idle_timeout_s:.0f}s — "
                        f"closing (Asterisk likely gone)"
                    )
                    state.call_ended = True
                    break
                if not data:
                    break  # connection closed

                for frame_type, payload in parser.feed(data):
                    if frame_type == AS_TYPE_HANGUP:
                        logger.info(f"[{call_sid}] AudioSocket hangup received")
                        return

                    elif frame_type == AS_TYPE_SLIN:
                        if state.call_ended:
                            continue

                        # Record incoming audio (caller side)
                        if recorder:
                            recorder.record_incoming(payload)

                        if state.speaking:
                            # Send silence (not actual echo) to Deepgram so its VAD
                            # pipeline stays warm during TTS playback.  If we send no
                            # audio at all, Deepgram goes cold and needs several warm-up
                            # windows before the first post-TTS utterance is recognised.
                            # Real microphone audio goes into the ring buffer so the
                            # energy-based barge-in engine can still work.
                            await conn_holder.send_audio(_SILENCE_FRAME)
                            await self._handle_frame_during_speech(
                                payload, conn_holder, audio_out, call_sid, state
                            )
                        else:
                            # Not speaking — forward all audio to Deepgram
                            state._hot_frames = 0
                            await conn_holder.send_audio(payload)
                            state._ambient_energies.append(_rms_energy(payload))

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{call_sid}] _recv_audiosocket error")
        finally:
            await audio_out.put(None)

    async def _handle_frame_during_speech(
        self,
        payload: bytes,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        """
        Process an audio frame while TTS is playing.

        Two-layer noise rejection (ZCR gate replaced by ambient-aware threshold):
          1. Adaptive energy threshold — max(min_floor, ambient×3, echo×4)
          2. Sustained confirmation + hysteresis — 120ms of above-threshold frames
        """
        state._ring_buffer.append(payload)
        energy = _rms_energy(payload)

        # Compute threshold from the PREVIOUS baseline BEFORE updating it.
        # This prevents a feedback loop where caller speech raises the baseline,
        # raising the threshold, making the caller’s own voice unable to trigger barge-in.
        ambient_floor = (
            sum(state._ambient_energies) / len(state._ambient_energies)
            if state._ambient_energies else 0.0
        )
        threshold = max(
            _BARGE_MIN_ENERGY,
            ambient_floor * _BARGE_AMBIENT_MULTIPLIER,
            state._echo_baseline * _BARGE_ECHO_MULTIPLIER,
        )

        # Only update echo baseline from quiet frames (below current threshold).
        # Frames above threshold are either caller speech or very loud echo — including
        # them would cause the baseline to chase the caller’s voice and kill barge-in.
        if energy < threshold:
            state._echo_energies.append(energy)
            if len(state._echo_energies) >= 5:
                state._echo_baseline = sum(state._echo_energies) / len(state._echo_energies)

        if _BARGE_DEBUG:
            logger.info(
                f"[{call_sid}] BARGE energy={energy:.0f} thr={threshold:.0f} "
                f"hot={state._hot_frames}/{_BARGE_CONFIRM_FRAMES} "
                f"hold={state._hold_frames} echo_base={state._echo_baseline:.0f} "
                f"ambient_floor={ambient_floor:.0f}"
            )

        # ── Layer 1: energy gate ───────────────────────────────────
        if energy <= threshold:
            state._hold_frames += 1
            if state._hold_frames > _BARGE_HOLD_FRAMES:
                state._hot_frames = 0
                state._hold_frames = 0
            return

        # ── Layer 2: sustained confirmation + hysteresis ─────────────
        # ZCR gate removed — the ambient-aware three-way threshold already
        # distinguishes impulse noise from voiced speech without ZCR overhead.
        state._hot_frames += 1
        state._hold_frames = 0

        if state._hot_frames >= _BARGE_CONFIRM_FRAMES:
            # ── BARGE-IN CONFIRMED ──
            # After end_call is signaled, the None sentinel is in the audio queue.
            # Allowing barge-in here would drain it away and stall call teardown.
            if state.end_call_requested:
                return

            logger.info(
                f"[{call_sid}] BARGE-IN detected | "
                f"hot={state._hot_frames} energy={energy:.0f} "
                f"threshold={threshold:.0f} "
                f"echo_baseline={state._echo_baseline:.0f} ambient_floor={ambient_floor:.0f}"
            )

            # 1. Signal barge-in → stops TTS production
            state.barge_in.set()

            # 2. Drain pending TTS audio from output queue
            _drain(audio_out)

            # 3. Cancel current turn
            if state.current_turn and not state.current_turn.done():
                state.current_turn.cancel()
            state.speaking = False
            state.processing = False

            # 4. Flush ring buffer to Deepgram — gives it the START
            #    of the user’s speech, not just the tail after detection
            buffered = list(state._ring_buffer)
            state._ring_buffer.clear()
            for frame in buffered:
                await conn_holder.send_audio(frame)

            # 5. Reset engine state
            state._hot_frames = 0
            state._hold_frames = 0
            state._echo_energies.clear()
            state._echo_baseline = 0.0
            state._speech_ended_at = 0.0  # no cooldown on barge-in

    async def _send_audiosocket(
        self,
        writer: asyncio.StreamWriter,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        recorder=None,
    ) -> None:
        """Reads PCM chunks from queue, wraps them in AudioSocket frames, and sends to Asterisk.
 
        Critical: the pacing loop must NOT call writer.drain() between frames.
        drain() yields to the event loop and waits for the TCP buffer to flush,
        introducing 5-20ms of jitter into the 20ms frame pacing.  Over many
        frames this makes the audio sound choppy / "walkie-talkie".
 
        With TCP_NODELAY set on the socket, the asyncio transport pushes data
        to the kernel automatically on each event loop iteration — drain() is
        only needed to prevent unbounded buffer growth (flow control), which
        never happens at our 16 KB/s data rate.
        """
        try:
            next_frame_time = time.monotonic()
            while True:
                pcm = await audio_out.get()
                if pcm is None:
                    break
                if state.call_ended:
                    continue
 
                # Audio pacing: send exactly 1 frame per 20ms.
                # AudioSocket is raw TCP with no jitter buffer — if we send
                # multiple frames in a burst, Asterisk plays them back-to-back
                # causing "fast-forward" distortion.  Never try to catch up;
                # if we fall behind, accept the slip and resume clean 20ms
                # pacing from now.
                now = time.monotonic()
                if next_frame_time > now:
                    await asyncio.sleep(next_frame_time - now)
                    next_frame_time += 0.020
                else:
                    # Behind schedule — send this frame now, resume 20ms from here
                    next_frame_time = now + 0.020
 
                frame = _make_audio_frame(pcm)
                writer.write(frame)
                if recorder:
                    recorder.record_outgoing(pcm)
            # Final drain: flush any remaining bytes in the transport buffer
            try:
                await writer.drain()
            except Exception:
                pass
            # Signal Asterisk to hang up and close the TCP connection immediately,
            # before post-processing begins.  Two steps are required:
            # 1. HANGUP frame (0xFF) — tells Asterisk's AudioSocket app to exit.
            # 2. writer.close() — closes the TCP socket so Asterisk drops the PSTN leg
            #    even if it doesn't react to the frame alone (version-dependent).
            # Without both, the PSTN call can stay alive through all of recording
            # upload + transcript saving (up to 15-20s of dead silence for the caller).
            try:
                from hangup import send_hangup_frame
                await send_hangup_frame(writer)
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{call_sid}] _send_audiosocket error")
 
        
                 

    # ── Transcript handler ────────────────────────────────────────────────

    async def _on_transcript(
        self,
        text: str,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
        conn_holder: _ConnHolder,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        if state.call_ended or state.end_call_requested:
            return
        active_orchestrator = orchestrator or self.orchestrator

        # Mark the moment Deepgram sent us a speech_final transcript
        stt_emit_time = time.monotonic()
        await _lat.event(
            call_sid, "USER_STOPPED",
            extra=f"text='{text[:50]}' lang={session.current_language}"
        )

        # ── Debounce duplicate transcripts ──
        now = stt_emit_time
        text = " ".join(text.split()).strip()
        if len(text) < _MIN_COMMIT_CHARS:
            logger.info(f"[{call_sid}] Dropping ultra-short transcript: '{text}'")
            return
        gap_ms = (now - state.last_time) * 1000
        if text.lower().strip() == state.last_text.lower().strip() and gap_ms < _DEBOUNCE_MS:
            return

        # ── Post-speech echo cooldown ──
        # After TTS ends, echo tail can produce short garbage transcripts.
        # During cooldown window, only accept substantial transcripts.
        if state._speech_ended_at > 0:
            elapsed = now - state._speech_ended_at
            if elapsed < _POST_SPEECH_COOLDOWN_S:
                if len(text.strip()) < _COOLDOWN_MIN_CHARS:
                    logger.info(
                        f"[{call_sid}] Cooldown drop ({elapsed*1000:.0f}ms, "
                        f"{len(text.strip())} chars): '{text}'"
                    )
                    return
            # Past cooldown window — clear the timestamp
            state._speech_ended_at = 0.0

        state.last_text = text
        state.last_time = now
        # Caller has responded — cancel any pending post-speech reminder.
        self._cancel_no_response_timer(state)
        logger.info(f"[{call_sid}] STT [{session.current_language}]: '{text}'")
        await _lat.event(
            call_sid, "STT_EMIT",
            t0=stt_emit_time,
            extra=f"text='{text[:50]}'"
        )

        # ── Language detection ──
        lang_code, switched = session.update_language(text)
        session.set("language", lang_code)

        if switched:
            logger.info(f"[{call_sid}] Language switched → {lang_code}")
            self.tts.set_language(lang_code)
            swap_task = asyncio.create_task(
                self._swap_deepgram(
                    lang_code,
                    conn_holder,
                    audio_out,
                    session,
                    call_sid,
                    state,
                    active_orchestrator,
                )
            )
            state.track_task(swap_task)

        # ── Transcript-based barge-in ──
        # Active when processing=True (LLM thinking, mic open, audio → Deepgram).
        # On barge-in we cancel immediately — no hold window — so the user's
        # correction is acted on right away.
        if state.processing:
            # Digit-continuation safety net: if the caller was mid-dictation
            # and paused longer than _DIGIT_HOLD_MS (so the partial already
            # committed), the second half arrives here as a "barge-in". If
            # merging it with the previously-committed turn yields a valid
            # 10-digit mobile, treat as continuation instead of correction.
            # Session history is safe to merge because the partial LLM call
            # was cancelled before add_message ran.
            merged_continuation: str | None = None
            if (
                session.current_agent == "hello"
                and not session.get("mobile")
                and state._last_committed_text
                and state._last_committed_text != text
            ):
                combined = (state._last_committed_text + " " + text).strip()
                res = parse_indian_mobile(combined, session.current_language or "hi")
                if res.digits and len(res.digits) == 10:
                    merged_continuation = combined
                    logger.info(
                        f"[{call_sid}] [DIGIT CONTINUATION] merging '{state._last_committed_text}' "
                        f"+ '{text}' → '{res.digits}'"
                    )

            # Address-continuation safety net — symmetric to digit-continuation.
            # If the first address fragment already committed (hold window was
            # too short or flag wasn't set yet) and the next fragment arrives
            # here as a "barge-in", merge instead of treating it as a correction.
            # Gated by `expecting_address` (or a still-extant address marker in
            # the prior commit) so we never merge unrelated interruptions.
            merged_address_continuation: str | None = None
            if (
                merged_continuation is None
                and session.current_agent == "service"
                and not session.get("address")
                and state._last_committed_text
                and state._last_committed_text != text
                and (
                    bool(session.get("expecting_address"))
                    or _looks_like_address_start(state._last_committed_text)
                )
            ):
                merged_address_continuation = (
                    state._last_committed_text + " " + text
                ).strip()
                session.set("expecting_address", True)
                logger.info(
                    f"[{call_sid}] [ADDRESS CONTINUATION] merging "
                    f"'{state._last_committed_text[:40]}...' + '{text[:40]}' → "
                    f"'{merged_address_continuation[:80]}'"
                )

            logger.info(f"[{call_sid}] Barge-in (transcript during processing): '{text[:40]}'")
            state.barge_in.set()
            _drain(audio_out)
            if state.current_turn and not state.current_turn.done():
                state.current_turn.cancel()
            state.processing = False
            state.speaking = False
            # Cancel any pending hold timer — start fresh with the barge-in text
            if state._hold_task and not state._hold_task.done():
                state._hold_task.cancel()
            state._pending_text = ""

            if merged_continuation:
                state._last_committed_text = ""  # consumed
                turn_text = merged_continuation
            elif merged_address_continuation:
                state._last_committed_text = ""  # consumed; will be re-set when merged turn commits
                turn_text = merged_address_continuation
            else:
                turn_text = text
            turn_task = asyncio.create_task(
                self._handle_turn(turn_text, audio_out, session, call_sid, state, active_orchestrator)
            )
            state.track_task(turn_task)
            return

        # ── Utterance commit buffer ──────────────────────────────────────────
        # Accumulate consecutive Deepgram speech_final fragments that arrive
        # within the hold window.  Digit-aware: while the caller is dictating
        # a mobile number, we extend the hold so natural pauses between digit
        # groups ("9663 8 ... 70199") don't commit a partial number.
        if state._hold_task and not state._hold_task.done():
            # Another fragment arrived before the window expired — extend.
            state._hold_task.cancel()
            state._pending_text = (state._pending_text + " " + text).strip()
            logger.info(
                f"[{call_sid}] Utterance hold extended: '{state._pending_text[:60]}'"
            )
        else:
            state._pending_text = text

        committed = state._pending_text   # snapshot for the closure

        # Decide hold window. Default is _UTTERANCE_HOLD_MS (150ms) — same as
        # before. In digit-dictation mode (hello agent, no mobile captured yet):
        #   • 10-digit number complete → commit immediately (0ms)
        #   • ≥1 partial digit        → extend to _DIGIT_HOLD_MS (2000ms)
        #   • 0 digits                → standard 150ms hold (confirmations, etc.)
        hold_ms = _UTTERANCE_HOLD_MS
        expecting_digits = (
            session.current_agent == "hello"
            and not session.get("mobile")
        )
        if expecting_digits:
            mobile_res = parse_indian_mobile(committed, session.current_language or "hi")
            if mobile_res.digits and len(mobile_res.digits) == 10:
                hold_ms = 0
                logger.info(
                    f"[{call_sid}] [DIGIT BUFFER] 10 digits complete → "
                    f"commit now: '{mobile_res.digits}'"
                )
            else:
                digit_count = count_parsed_digits(committed)
                if digit_count >= 1:
                    hold_ms = _DIGIT_HOLD_MS
                    logger.info(
                        f"[{call_sid}] [DIGIT BUFFER] partial ({digit_count} digits) "
                        f"→ holding {hold_ms}ms for continuation"
                    )
        elif session.current_agent == "service" and not session.get("address") and (
            bool(session.get("expecting_address"))
            or _looks_like_address_start(committed)
        ):
            # Either the service agent asked for address and set the flag, OR
            # the caller self-initiated with phrases like "मेरा पूरा पता है...".
            # Latch the flag so subsequent fragments in this dictation stay in
            # extended-hold mode until the address is captured by the agent.
            hold_ms = _ADDRESS_HOLD_MS
            session.set("expecting_address", True)
            logger.info(
                f"[{call_sid}] [ADDRESS BUFFER] extending {hold_ms}ms: "
                f"'{committed[:80]}'"
            )
        async def _commit_turn() -> None:
            try:
                if hold_ms > 0:
                    await asyncio.sleep(hold_ms / 1000)
            except asyncio.CancelledError:
                return  # another fragment arrived — hold was extended
            if not state.call_ended:
                logger.info(f"[{call_sid}] Utterance committed: '{committed[:60]}'")
                state._last_committed_text = committed
                t = asyncio.create_task(
                    self._handle_turn(
                        committed,
                        audio_out,
                        session,
                        call_sid,
                        state,
                        active_orchestrator,
                    )
                )
                state.track_task(t)

        hold_task = asyncio.create_task(_commit_turn())
        state._hold_task = hold_task
        state.track_task(hold_task)

    # ── One conversation turn ─────────────────────────────────────────────

    async def _handle_turn(
        self,
        text: str,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
        orchestrator: Orchestrator,
    ) -> None:
        if state.processing:
            return

        # Any new turn start means we no longer need a silence reminder from the
        # previous agent utterance.
        self._cancel_no_response_timer(state)
        state.processing = True
        my_task = asyncio.current_task()
        state.current_turn = my_task
        state.barge_in.clear()

        turn_start = time.monotonic()
        turn_spoke = False
        try:
            # Deterministic replay path:
            # If caller says "voice cut / repeat", replay the previous assistant
            # step exactly instead of re-running agent-specific capture logic.
            if _is_repeat_previous_step_request(text):
                last_step = _last_assistant_step(session)
                if last_step:
                    # Prefer the script of the *current* transcript over the
                    # session's stored language, which may be stale when the
                    # caller just switched back to Hindi after an English stretch.
                    if re.search(r"[ऀ-ॿ]", text):
                        lang = "hi"
                    else:
                        lang = (session.current_language or session.get("language") or "hi").lower()
                    replay = f"{_repeat_prefix(lang)} {last_step}".strip()
                    logger.info(f"[{call_sid}] [REPEAT STEP] Replaying previous assistant step")
                    session.add_message("user", text)
                    session.add_message("assistant", replay)
                    await _lat.event(call_sid, "REPEAT_STEP", t0=turn_start, extra=f"text='{text[:50]}'")
                    await self._speak(replay, audio_out, call_sid, state, session=session)
                    turn_spoke = True
                    return

            llm_start = time.monotonic()
            await _lat.event(call_sid, "LLM_START", t0=turn_start,
                             extra=f"input='{text[:50]}'")
            logger.info(f"[{call_sid}] [LAT] LLM start")

            # ── Streaming LLM → TTS pipeline ──────────────────────────────
            # LLM and TTS run serially (TTS blocks the LLM generator between
            # sentences) to ensure stable 20ms audio pacing. Decoupling them
            # causes event loop contention that disrupts AudioSocket timing.
            # The "LLM done" timer includes TTS time — actual perceived latency
            # is much lower (user hears first sentence while LLM still generates).
            reply_parts: list[str] = []
            final_response = None
            tts_started = False
            first_sentence_flushed = False
            pending_tts_parts: list[str] = []

            async def _flush_tts_batch() -> None:
                nonlocal tts_started, pending_tts_parts, turn_spoke
                if not pending_tts_parts or state.barge_in.is_set():
                    return
                batched_text = _sanitize_spoken_text(" ".join(pending_tts_parts).strip())
                pending_tts_parts = []
                if not batched_text:
                    return
                if session is not None:
                    _lang = (
                        getattr(session, "current_language", None)
                        or session.get("language")
                        or "hi"
                    )
                    if _lang == "hi":
                        batched_text = _replace_digits_hindi(batched_text)
                if not tts_started:
                    state.reset_barge_engine()
                    state.speaking = True
                    for _ in range(_LEAD_SILENCE_FRAMES):
                        if state.barge_in.is_set():
                            break
                        await audio_out.put(_SILENCE_FRAME)
                    tts_started = True
                    turn_spoke = True
                await self._tts_to_queue(batched_text, audio_out, call_sid, state, session=session)

            async for sentence, resp in orchestrator.stream_process(text, session):
                if state.barge_in.is_set():
                    break

                if sentence:
                    reply_parts.append(sentence)
                    pending_tts_parts.append(sentence)
                    batched_text = " ".join(pending_tts_parts)
                    should_flush = (
                        not first_sentence_flushed
                        or len(pending_tts_parts) >= _TTS_BATCH_MAX_SENTENCES
                        or len(batched_text) >= _TTS_BATCH_MAX_CHARS
                        or sentence.strip().endswith("?")
                    )
                    if should_flush:
                        first_sentence_flushed = True
                        await _flush_tts_batch()

                if resp is not None:
                    await _flush_tts_batch()
                    final_response = resp
                    llm_ms = (time.monotonic() - llm_start) * 1000
                    full_reply = " ".join(reply_parts)
                    await _lat.event(call_sid, "LLM_DONE", t0=llm_start,
                                     extra=f"llm_ms={llm_ms:.0f} reply='{full_reply[:60]}'")
                    logger.info(
                        f"[{call_sid}] [LAT] LLM done in {llm_ms:.0f}ms | "
                        f"Reply [{session.current_language}]: '{full_reply[:100]}'"
                    )

            await _flush_tts_batch()

            # Trail silence
            if not state.barge_in.is_set():
                for _ in range(_TRAIL_SILENCE_FRAMES):
                    if state.barge_in.is_set():
                        break
                    await audio_out.put(_SILENCE_FRAME)

            turn_ms = (time.monotonic() - turn_start) * 1000
            await _lat.event(call_sid, "TURN_COMPLETE", t0=turn_start,
                             extra=f"total_turn_ms={turn_ms:.0f}")
            logger.info(f"[{call_sid}] [LAT] Turn complete in {turn_ms:.0f}ms total")

            if final_response and final_response.end_call:
                # Set end_call_requested BEFORE queuing the None so that any barge-in
                # or noise transcript that arrives while the goodbye TTS is draining
                # cannot call _drain(audio_out) and silently remove the None sentinel.
                # Do NOT set state.call_ended here — that would cause remaining TTS
                # frames to be skipped in _send_audiosocket.
                state.end_call_requested = True
                await audio_out.put(None)

        except asyncio.CancelledError:
            logger.info(f"[{call_sid}] Turn cancelled by barge-in")
            # Preserve what the agent had generated up to the interruption so the
            # LLM doesn't repeat itself on the next turn. The trailing "…" marks
            # the message as truncated; the user message gets added by whatever
            # path drives the next turn (transcript handler appends user text
            # before the next _handle_turn fires for the barge-in transcript).
            partial = (session.get("_partial_assistant") or "").strip()
            if partial:
                clean_partial = _sanitize_spoken_text(_CONTROL_RE.sub("", partial)).strip()
                if clean_partial:
                    session.add_message("user", text)
                    session.add_message("assistant", clean_partial + " …")
                    logger.info(
                        f"[{call_sid}] Saved partial assistant turn on barge-in "
                        f"({len(clean_partial)} chars)"
                    )
            session.set("_partial_assistant", "")

        except Exception:
            logger.exception(f"[{call_sid}] _handle_turn error")

        finally:
            # Only reset if WE are still the active turn.
            if state.current_turn is my_task:
                # Wait for audio to physically finish playing before opening mic
                while not audio_out.empty() and not state.barge_in.is_set():
                    await asyncio.sleep(0.05)
                if not state.barge_in.is_set():
                    state._speech_ended_at = time.monotonic()
                    if turn_spoke and not state.end_call_requested:
                        self._schedule_no_response_timer(audio_out, session, call_sid, state)
                state.speaking = False
                state.processing = False
                state.current_turn = None

    # ── TTS helpers ───────────────────────────────────────────────────────

    async def _speak(
        self,
        text: str,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        clean = _CONTROL_RE.sub("", text).strip()
        clean = _sanitize_spoken_text(clean)
        if not clean:
            return

        # Convert standalone digits 0–10 and English number words to Hindi so
        # TTS never reads them in English during a Hindi conversation.
        # Use current_language (live tracker) first; fall back to stored metadata.
        if session is not None:
            lang = (
                getattr(session, "current_language", None)
                or session.get("language")
                or "hi"
            )
            if lang == "hi":
                clean = _replace_digits_hindi(clean)

        sentences = [s.strip() for s in _SENTENCE_RE.split(clean) if s.strip()] or [clean]

        # Reset barge-in engine for this utterance
        state.reset_barge_engine()
        state.speaking = True  # NOW gate the mic — TTS is about to play

        try:
            # Lead silence
            for _ in range(_LEAD_SILENCE_FRAMES):
                if state.barge_in.is_set():
                    return
                await audio_out.put(_SILENCE_FRAME)

            for sentence in sentences:
                if state.barge_in.is_set():
                    break
                await self._tts_to_queue(sentence, audio_out, call_sid, state, session=session)

            # Trail silence
            for _ in range(_TRAIL_SILENCE_FRAMES):
                if state.barge_in.is_set():
                    return
                await audio_out.put(_SILENCE_FRAME)

        finally:
            # Wait for audio to physically finish playing before opening mic
            while not audio_out.empty() and not state.barge_in.is_set():
                await asyncio.sleep(0.05)
            state.speaking = False

    async def _tts_to_queue(
        self,
        text: str,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        # Convert digit-word runs to paired numerics before TTS sees the text.
        # e.g. "nine eight seven six five four three two one zero" → "98 76 54 32 10"
        text = _normalize_digit_words(text)
        tts_start = time.monotonic()
        # Use persistent call session if available (sonic-3), else fall back
        _tts_source = state.call_tts if state.call_tts is not None else self.tts
        
        # Ensure TTS uses the correct session language right before synthesis
        if session:
            _lang = session.current_language
            if hasattr(_tts_source, "set_language"):
                _tts_source.set_language(_lang)
            elif hasattr(_tts_source, "language"):
                _tts_source.language = _lang

        _mode = "persistent" if state.call_tts is not None else "one-shot"
        # NEW: read adaptive TTS rate from session (set by ServiceAgent.handle)
        _tts_rate = float(session.get("tts_rate_multiplier", 1.0)) if session else 1.0
        await _lat.event(call_sid, "TTS_START", t0=tts_start,
                         extra=f"mode={_mode} text='{text[:50]}'")
        logger.info(f"[{call_sid}] [LAT] TTS start ({_mode}): '{text[:60]}'")
        first_chunk = True
        chunk_count = 0
        leftover = b""  # buffer for partial frames across TTS chunks
        pending_frames: deque[bytes] = deque()
        startup_buffered = False
        provider_name = type(_tts_source).__name__.lower()
        # Always stream. stream_synthesize() internally uses the persistent WS
        # (_call_ws) when available (TTFB ~150ms) — far faster than REST one-shot
        # which blocks 700-4000ms. The one-shot path is never beneficial.
        use_one_shot = False

        async def _flush_frames(force: bool = False) -> None:
            nonlocal startup_buffered
            if not startup_buffered:
                if force or len(pending_frames) >= _TTS_STARTUP_PREBUFFER_FRAMES:
                    startup_buffered = True
                else:
                    return
 
            keep = 0 if force else _TTS_STEADY_RESERVE_FRAMES
            while len(pending_frames) > keep:
                if state.barge_in.is_set():
                    return
                await audio_out.put(pending_frames.popleft())
 

        async def _queue_pcm_blob(pcm_blob: bytes) -> None:
            nonlocal leftover
            data = leftover + pcm_blob
            leftover = b""
            for i in range(0, len(data), _AS_FRAME_BYTES):
                if state.barge_in.is_set():
                    return
                frame = data[i: i + _AS_FRAME_BYTES]
                if len(frame) == _AS_FRAME_BYTES:
                    pending_frames.append(frame)
                else:
                    leftover = frame
 
        try:
            if use_one_shot:
                pcm_blob = await _tts_source.synthesize(text)
                if state.barge_in.is_set():
                    return
                fc_ms = (time.monotonic() - tts_start) * 1000
                await _lat.event(call_sid, "TTS_FIRST_CHUNK", t0=tts_start,
                                 extra=f"ttfc_ms={fc_ms:.0f} mode=one-shot")
                logger.info(f"[{call_sid}] [LAT] TTS one-shot ready in {fc_ms:.0f}ms")
                first_chunk = False
                chunk_count = 1
                await _queue_pcm_blob(pcm_blob)
                await _flush_frames(force=True)
            else:
                # Hold a reference to the generator so we can explicitly close
                # it on barge-in.  Closing propagates to _ws_stream_reuse's
                # finally block which calls ctx.cancel() — preventing orphaned
                # context queues from accumulating on the persistent WebSocket.
                stream = _tts_source.stream_synthesize(text, language_code=_lang if session else None)
                try:
                    async for chunk in stream:
                        if state.barge_in.is_set():
                            break
                        if first_chunk:
                            fc_ms = (time.monotonic() - tts_start) * 1000
                            await _lat.event(call_sid, "TTS_FIRST_CHUNK", t0=tts_start,
                                             extra=f"ttfc_ms={fc_ms:.0f} mode=stream")
                            logger.info(f"[{call_sid}] [LAT] TTS first chunk in {fc_ms:.0f}ms")
                            first_chunk = False
                        chunk_count += 1
                        await _queue_pcm_blob(chunk)
                        await _flush_frames()
                finally:
                    await stream.aclose()
                if state.barge_in.is_set():
                    return
            
            if leftover and not state.barge_in.is_set():
                pending_frames.append(leftover.ljust(_AS_FRAME_BYTES, b'\x00'))
 
            if pending_frames and not state.barge_in.is_set():
                await _flush_frames(force=True)
                
            if not state.barge_in.is_set():
                tts_ms = (time.monotonic() - tts_start) * 1000
                await _lat.event(call_sid, "TTS_DONE", t0=tts_start, extra=f"tts_ms={tts_ms:.0f} chunks={chunk_count}")
                logger.info(
                    f"[{call_sid}] [LAT] TTS stream complete in {tts_ms:.0f}ms "
                    f"({chunk_count} chunks) — NOTE: this is audio duration, "
                    f"not latency. User heard first word at TTFB above."
                )
        except asyncio.CancelledError:
            logger.info(f"[{call_sid}] TTS cancelled (barge-in)")
        except Exception:
            logger.exception(f"[{call_sid}] TTS stream error")
 
 
