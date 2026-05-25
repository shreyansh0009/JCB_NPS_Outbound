"""
CallerPaceProfiler — measures caller's speaking pace from Deepgram transcripts
and exposes a CallerPace enum (FAST / NORMAL / SLOW) for adaptive STT + TTS tuning.

Usage:
    profiler = CallerPaceProfiler()

    # on Deepgram SpeechStarted event:
    profiler.mark_turn_start()

    # on final transcript:
    profiler.record_transcript(text)

    # before TTS / endpointing decisions:
    pace = profiler.current_pace          # CallerPace enum
    rate = profiler.tts_rate_multiplier   # float, e.g. 1.20
    ep   = profiler.endpointing_ms        # int,   e.g. 300
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class CallerPace(Enum):
    FAST   = "fast"    # >3.5 words/sec
    NORMAL = "normal"  # 2.0–3.5 words/sec
    SLOW   = "slow"    # <2.0 words/sec


# ── Thresholds ────────────────────────────────────────────────────────────────
_FAST_WPS  = 3.5   # above this → FAST
_SLOW_WPS  = 2.0   # below this → SLOW

# ── Adaptive Deepgram endpointing_ms per pace ─────────────────────────────────
_ENDPOINTING_MS: dict[CallerPace, int] = {
    CallerPace.FAST:   500,   # survey context — even fast callers pause mid-sentence; 500ms prevents premature cuts
    CallerPace.NORMAL: 700,   # balanced default for natural speech with mid-sentence pauses
    CallerPace.SLOW:   900,   # slow callers take long pauses within a sentence; wait longer before committing
}

# ── Adaptive FORCE_EMIT_TIMEOUT per pace (for is_final-only fragments) ────────
_FORCE_EMIT_TIMEOUT: dict[CallerPace, float] = {
    CallerPace.FAST:   0.4,   # fast caller: short wait before force-emitting fragment
    CallerPace.NORMAL: 0.8,   # matches current FORCE_EMIT_TIMEOUT default
    CallerPace.SLOW:   0.7,   # was 1.2 — 700ms still patient without stacking with endpointing
}

# ── Adaptive TTS rate multiplier per pace ─────────────────────────────────────
_TTS_RATE: dict[CallerPace, float] = {
    CallerPace.FAST:   1.20,   # agent speaks 20% faster to match fast caller's energy
    CallerPace.NORMAL: 1.00,   # baseline agent speed
    CallerPace.SLOW:   0.85,   # agent speaks 15% slower — clearer for slow callers
}

# ── SSML prosody rate strings (for TTS providers that support SSML) ───────────
_SSML_RATE: dict[CallerPace, str] = {
    CallerPace.FAST:   "fast",
    CallerPace.NORMAL: "medium",
    CallerPace.SLOW:   "slow",
}


@dataclass
class CallerPaceProfiler:
    """
    Rolling-window WPS profiler. Thread-safe for asyncio usage.

    Args:
        window_size: number of recent turns to average over (default 5)
    """
    window_size: int = 5

    _samples: deque = field(init=False)
    _turn_start: float = field(init=False, default=0.0)

    def __post_init__(self):
        self._samples = deque(maxlen=self.window_size)

    # ── Public API ────────────────────────────────────────────────────────────

    def mark_turn_start(self) -> None:
        """Call when Deepgram SpeechStarted fires (VAD on)."""
        self._turn_start = time.monotonic()

    def record_transcript(self, transcript: str) -> None:
        """Call when a final Deepgram transcript arrives."""
        if not self._turn_start:
            return
        elapsed   = time.monotonic() - self._turn_start
        words     = transcript.strip().split()
        word_count = len(words)
        # Ignore very short fragments — they skew WPS calculation
        if elapsed > 0.3 and word_count >= 2:
            wps = word_count / elapsed
            self._samples.append(wps)
        self._turn_start = 0.0

    @property
    def current_pace(self) -> CallerPace:
        avg = self.avg_wps
        if avg >= _FAST_WPS:
            return CallerPace.FAST
        elif avg <= _SLOW_WPS:
            return CallerPace.SLOW
        return CallerPace.NORMAL

    @property
    def avg_wps(self) -> float:
        """Current rolling average words-per-second."""
        if not self._samples:
            return 2.5   # assume NORMAL until we have data
        return sum(self._samples) / len(self._samples)

    @property
    def endpointing_ms(self) -> int:
        """Adaptive Deepgram endpointing_ms for the current pace."""
        return _ENDPOINTING_MS[self.current_pace]

    @property
    def force_emit_timeout(self) -> float:
        """Adaptive FORCE_EMIT_TIMEOUT for the current pace."""
        return _FORCE_EMIT_TIMEOUT[self.current_pace]

    @property
    def tts_rate_multiplier(self) -> float:
        """Adaptive TTS speaking rate multiplier."""
        return _TTS_RATE[self.current_pace]

    @property
    def ssml_rate(self) -> str:
        """SSML prosody rate string for the current pace."""
        return _SSML_RATE[self.current_pace]

    def wrap_ssml(self, text: str) -> str:
        """Wraps TTS text in SSML <prosody rate> tag."""
        rate = self.ssml_rate
        return f'<speak><prosody rate="{rate}">{text}</prosody></speak>'

    def __repr__(self) -> str:
        return (
            f"CallerPaceProfiler(pace={self.current_pace.value}, "
            f"avg_wps={self.avg_wps:.2f}, samples={list(self._samples)})"
        )