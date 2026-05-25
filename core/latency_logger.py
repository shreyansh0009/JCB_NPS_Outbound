"""
LatencyLogger — zero-latency async file logger for voice pipeline timing.

Design goals
------------
* NEVER block the event loop (no synchronous file I/O on hot paths).
* NEVER slow down audio processing (all writes happen off the call's
  critical path via a background writer coroutine).
* Persistent across calls — appends to latency.log in the project root.
* Produces structured, human-readable lines that are easy to grep / tail.

Usage
-----
    from core.latency_logger import get_latency_logger

    log = get_latency_logger()          # singleton — call anywhere
    await log.start()                   # once at app startup

    # Mark an event (async, non-blocking):
    await log.event(call_sid, "STT_FINAL", t0=stt_start_time)

    # Mark a duration (async, non-blocking):
    await log.event(call_sid, "LLM_DONE", t0=llm_start_time)

    await log.stop()                    # once at app shutdown

Log format (one line per event):
    2026-03-31 17:14:57.123 | CALL-abc | STT_FINAL      |   342ms since prev

Labels used by StreamingPipeline
---------------------------------
    USER_STOPPED        — Deepgram speech_final received (user finished speaking)
    STT_EMIT            — transcript forwarded to pipeline
    LLM_START           — orchestrator.process() begins
    LLM_DONE            — orchestrator.process() returned; reply text ready
    TTS_START           — first TTS chunk requested
    TTS_FIRST_CHUNK     — first audio chunk received from TTS API
    TTS_DONE            — last TTS chunk queued for playback
    TURN_COMPLETE       — full turn finished (TTS fully queued)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path to the log file (relative to the project root)
_LOG_PATH = Path(__file__).resolve().parent.parent / "latency.log"

# Maximum queue depth before we start dropping events
# (prevents memory growth if the writer falls behind — should never happen)
_QUEUE_MAX = 2000


class LatencyLogger:
    """
    Singleton async latency logger.

    All public methods are coroutines so callers can ``await`` them on the
    hot path, but the actual file I/O happens in a separate background task.
    """

    def __init__(self, log_path: Path = _LOG_PATH):
        self._path = log_path
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._writer_task: Optional[asyncio.Task] = None
        self._started = False
        # Per-(call_sid) last-event timestamp for "Δms" column
        self._last_ts: dict[str, float] = {}

    async def start(self) -> None:
        """Start the background writer task. Call once at app startup."""
        if self._started:
            return
        self._started = True
        self._writer_task = asyncio.create_task(self._writer_loop(), name="latency-writer")
        logger.info(f"LatencyLogger started → {self._path}")

    async def stop(self) -> None:
        """Flush and stop the background writer. Call at app shutdown."""
        if not self._started:
            return
        await self._queue.put(None)  # sentinel
        if self._writer_task:
            try:
                await asyncio.wait_for(self._writer_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._started = False

    async def event(
        self,
        call_sid: str,
        label: str,
        t0: Optional[float] = None,
        extra: str = "",
    ) -> None:
        """
        Record a latency event non-blockingly.

        Parameters
        ----------
        call_sid : str
            Unique call identifier.
        label : str
            Event name, e.g. "STT_FINAL", "LLM_START".
        t0 : float, optional
            ``time.monotonic()`` reference point.  When provided, the log
            line includes both "Δms since t0" and "Δms since last event".
        extra : str, optional
            Any additional data to append to the line (no newlines).
        """
        if not self._started:
            return  # silently skip if logger not started yet

        now = time.monotonic()
        wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # Δ since provided t0
        delta_t0 = f"{(now - t0)*1000:7.1f}ms" if t0 is not None else "       -"

        # Δ since previous event for this call
        prev = self._last_ts.get(call_sid)
        delta_prev = f"{(now - prev)*1000:7.1f}ms" if prev is not None else "       -"
        self._last_ts[call_sid] = now

        line = (
            f"{wall} | {call_sid[:12]:<12} | {label:<18} "
            f"| Δt0={delta_t0} | Δprev={delta_prev}"
        )
        if extra:
            line += f" | {extra}"
        line += "\n"

        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # Writer is somehow stuck — drop the event rather than block
            pass

    async def separator(self, call_sid: str, title: str = "") -> None:
        """Write a visual separator line (e.g. start/end of a call)."""
        wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        bar = "─" * 80
        line = f"\n{bar}\n{wall} | {call_sid[:12]:<12} | {title}\n{bar}\n"
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass

    def clear_call(self, call_sid: str) -> None:
        """Remove per-call timing state (call clean-up)."""
        self._last_ts.pop(call_sid, None)

    # ── Background writer ──────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        """
        Drains the event queue and writes to disk.

        Uses asyncio.to_thread so the blocking open/write never occupies
        the event loop — even a slow disk or NFS mount won't stall audio.
        """
        try:
            while True:
                line = await self._queue.get()
                if line is None:
                    break  # stop sentinel
                # Collect any extra pending items in one batch for efficiency
                batch = [line]
                while not self._queue.empty():
                    item = self._queue.get_nowait()
                    if item is None:
                        # Put sentinel back and break
                        self._queue.put_nowait(None)
                        break
                    batch.append(item)

                content = "".join(batch)
                await asyncio.to_thread(self._write_sync, content)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("LatencyLogger writer_loop crashed")

    def _write_sync(self, content: str) -> None:
        """Synchronous file write (runs in a thread pool via asyncio.to_thread)."""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:
            logger.error(f"LatencyLogger write error: {exc}")


# ── Module-level singleton ────────────────────────────────────────────────────

_instance: Optional[LatencyLogger] = None


def get_latency_logger() -> LatencyLogger:
    """Return the process-wide LatencyLogger singleton."""
    global _instance
    if _instance is None:
        _instance = LatencyLogger()
    return _instance
