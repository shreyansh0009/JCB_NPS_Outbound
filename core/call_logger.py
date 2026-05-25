"""
CallTraceLogger — per-call performance tracing.

Writes a fresh log file for each call. When a new call starts, the previous
call's logs are wiped and replaced with the new call's trace.

Tracks timing for every step: STT transcripts, LLM requests, TTS synthesis,
RAG retrieval, MCP tool calls, barge-in events, and full turn round-trips.

At call end, prints a platform summary showing total time, hit count, and
per-hit breakdown for each external service (STT, LLM, TTS, RAG, MCP).

Usage:
    from core.call_logger import call_trace

    # At call start (clears old logs):
    call_trace.new_call(call_sid)

    # Log a timed step:
    call_trace.log("LLM", "chat request", duration_ms=142.3, detail="model=llama-3.3-70b")

    # Or use the timer context manager:
    with call_trace.timer("TTS", "stream_synthesize"):
        await tts.stream_synthesize(text)

    # At call end:
    call_trace.end_call()
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "call_trace.log"

# Ensure directory exists
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Platforms tracked in the summary (order matters for display)
_TRACKED_PLATFORMS = ["STT", "LLM", "TTS", "RAG", "MCP", "ORCHESTR", "TURN"]


@dataclass
class _HitRecord:
    """One timed API hit."""
    event: str
    duration_ms: float
    detail: str


class CallTraceLogger:
    """Singleton-style per-call trace logger."""

    def __init__(self):
        self._call_sid: str = ""
        self._call_start: float = 0.0
        self._turn_count: int = 0
        self._stats: dict[str, list[_HitRecord]] = {}
        self._logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("call_trace")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        # Remove old handlers
        for h in logger.handlers[:]:
            logger.removeHandler(h)
        # File handler — will be reset on each new call
        fh = logging.FileHandler(_LOG_FILE, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(fh)
        return logger

    def new_call(self, call_sid: str) -> None:
        """Start tracing a new call. Clears all previous logs."""
        self._call_sid = call_sid
        self._call_start = time.monotonic()
        self._turn_count = 0
        self._stats = {}

        # Reset file handler to truncate the file
        for h in self._logger.handlers[:]:
            self._logger.removeHandler(h)
            h.close()
        fh = logging.FileHandler(_LOG_FILE, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(fh)

        self._write_header()

    def _write_header(self) -> None:
        self._logger.info("=" * 90)
        self._logger.info(f"  CALL TRACE — {self._call_sid}")
        self._logger.info(f"  Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._logger.info("=" * 90)
        self._logger.info("")
        self._logger.info(
            f"{'ELAPSED':>10}  {'COMPONENT':<12} {'EVENT':<34} {'DURATION':>10}  DETAILS"
        )
        self._logger.info(
            f"{'─' * 10}  {'─' * 12} {'─' * 34} {'─' * 10}  {'─' * 30}"
        )

    def _elapsed(self) -> str:
        """Time since call start in seconds."""
        if self._call_start == 0:
            return "    -    "
        elapsed = time.monotonic() - self._call_start
        return f"{elapsed:>9.3f}s"

    def log(
        self,
        component: str,
        event: str,
        duration_ms: float | None = None,
        detail: str = "",
    ) -> None:
        """Log a trace event and track stats for timed events."""
        dur = f"{duration_ms:>8.1f}ms" if duration_ms is not None else "         -"
        line = f"{self._elapsed()}  {component:<12} {event:<34} {dur}  {detail}"
        self._logger.info(line)

        # Accumulate stats for platforms with duration
        if duration_ms is not None:
            platform = component.upper()
            if platform not in self._stats:
                self._stats[platform] = []
            self._stats[platform].append(
                _HitRecord(event=event, duration_ms=duration_ms, detail=detail)
            )

    def log_divider(self, label: str = "") -> None:
        """Visual separator for turns."""
        if label:
            self._logger.info(f"\n{'─' * 20} {label} {'─' * (68 - len(label))}")
        else:
            self._logger.info("")

    def new_turn(self, transcript: str) -> None:
        """Mark the start of a new conversation turn (user → agent)."""
        self._turn_count += 1
        self.log_divider(f"TURN {self._turn_count} (user \u2192 agent)")
        self.log("USER", "transcript received", detail=f"'{transcript[:80]}'")

    @contextmanager
    def timer(self, component: str, event: str, detail: str = ""):
        """Context manager that auto-logs duration."""
        start = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            self.log(component, event, duration_ms=duration_ms, detail=detail)

    def end_call(self) -> None:
        """Log call end with summary and platform breakdown."""
        total_s = time.monotonic() - self._call_start if self._call_start else 0
        self._logger.info("")
        self._logger.info("=" * 90)
        self._logger.info(f"  CALL ENDED — {self._call_sid}")
        self._logger.info(f"  Total duration: {total_s:.1f}s | Turns: {self._turn_count}")
        self._logger.info(f"  Ended: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._logger.info("=" * 90)

        self._write_summary()

    def _write_summary(self) -> None:
        """Write platform summary and per-hit breakdown at end of call."""
        if not self._stats:
            return

        w = self._logger.info
        w("")
        w(f"  \u250c{'─' * 68}\u2510")
        w(f"  \u2502{'PLATFORM SUMMARY':^68}\u2502")
        w(f"  \u251c{'─' * 68}\u2524")
        w(f"  \u2502 {'PLATFORM':<12} {'HITS':>5}  {'TOTAL TIME':>12}  {'AVG':>10}  {'MIN':>10}  {'MAX':>10} \u2502")
        w(f"  \u251c{'─' * 68}\u2524")

        # Display in defined order, then any extras
        platforms = []
        for p in _TRACKED_PLATFORMS:
            if p in self._stats:
                platforms.append(p)
        for p in self._stats:
            if p not in platforms:
                platforms.append(p)

        for platform in platforms:
            hits = self._stats[platform]
            count = len(hits)
            total_ms = sum(h.duration_ms for h in hits)
            avg_ms = total_ms / count if count else 0
            min_ms = min(h.duration_ms for h in hits) if hits else 0
            max_ms = max(h.duration_ms for h in hits) if hits else 0
            w(
                f"  \u2502 {platform:<12} {count:>5}  "
                f"{total_ms:>10.1f}ms  {avg_ms:>8.1f}ms  "
                f"{min_ms:>8.1f}ms  {max_ms:>8.1f}ms \u2502"
            )

        w(f"  \u2514{'─' * 68}\u2518")

        # Per-hit breakdown
        w("")
        w(f"  \u250c{'─' * 68}\u2510")
        w(f"  \u2502{'PER-HIT BREAKDOWN':^68}\u2502")
        w(f"  \u251c{'─' * 68}\u2524")

        for platform in platforms:
            hits = self._stats[platform]
            w(f"  \u2502 {platform:<67}\u2502")
            for i, hit in enumerate(hits, 1):
                evt = hit.event[:30]
                detail_str = f"  {hit.detail[:25]}" if hit.detail else ""
                w(
                    f"  \u2502   #{i:<3} {evt:<30} {hit.duration_ms:>8.1f}ms"
                    f"{detail_str:<27}\u2502"
                )
            if platform != platforms[-1]:
                w(f"  \u251c{'─' * 68}\u2524")

        w(f"  \u2514{'─' * 68}\u2518")


# Module-level singleton
call_trace = CallTraceLogger()
