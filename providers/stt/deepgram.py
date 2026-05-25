"""
DeepgramSTT — AudioSocket-compatible streaming speech-to-text, multilingual.
 
AudioSocket sends slin16 PCM (linear16) audio at 8kHz.
Deepgram accepts linear16 8kHz natively — no local re-encoding needed.
 
Multilingual support
--------------------
nova-2 supports Hindi (hi), Bengali (bn), Tamil (ta), Telugu (te),
Marathi (mr), Gujarati (gu), Kannada (kn), Punjabi (pa), Malayalam (ml).
 
For multilingual auto-detect: use model=nova-2 with NO language param +
detect_language=true. Deepgram will detect and transcribe accordingly.
 
For pinned language (after user switches): use model=nova-2 + language=hi.
This gives the best accuracy for that specific language.
 
Short-word detection strategy:
  PATH A — speech_final=True fires  -> emit immediately
  PATH C — is_final=True but speech_final=False -> start 800ms force-emit timer
"""
from __future__ import annotations
 
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

 
import websockets
 
from core.call_logger import call_trace
from core.circuit_breaker import CircuitBreakerRegistry
 
logger = logging.getLogger(__name__)
 
FORCE_EMIT_TIMEOUT = 0.8   # seconds — wait 800ms before emitting is_final-only fragments
                            # (was 0.4 — too short; user continuations after 400ms were lost)
 
 
class DeepgramConnection:
    """Active connection to Deepgram."""
 
    def __init__(
        self,
        ws,
        on_transcript: Callable[[str], Awaitable[None]],
        on_speech_start: Optional[Callable[[], Awaitable[None]]] = None,
        get_force_emit_timeout: Optional[Callable[[], float]] = None,
    ):
        self._ws = ws
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._get_force_emit_timeout = get_force_emit_timeout  # NEW: callable → adaptive timeout
        self._pending: str | None = None
        self._force_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
 
    async def send_audio(self, mulaw_bytes: bytes) -> None:
        try:
            await self._ws.send(mulaw_bytes)
        except Exception as e:
            logger.debug(f"Deepgram send_audio error: {e}")
 
    async def close(self) -> None:
        if self._force_task and not self._force_task.done():
            self._force_task.cancel()
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass
 
    def _start_receive(self) -> None:
        self._receive_task = asyncio.create_task(self._receive_loop())
 
    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._handle(msg)
        except Exception as e:
            logger.debug(f"Deepgram receive loop ended: {e}")
 
    async def _handle(self, msg: dict) -> None:
        typ = msg.get("type", "")
 
        if typ == "SpeechStarted":
            if self._on_speech_start:
                await self._on_speech_start()
 
        elif typ == "Results":
            alts = msg.get("channel", {}).get("alternatives", [])
            text = alts[0].get("transcript", "").strip() if alts else ""
            is_final     = msg.get("is_final", False)
            speech_final = msg.get("speech_final", False)
 
            if not text:
                return
 
            if is_final and speech_final:
                # Combine with any pending fragment from prior is_final-only events
                if self._pending and self._pending.lower() != text.lower():
                    text = (self._pending + " " + text).strip()
                self._cancel_force_emit()
                self._pending = None
                await self._emit(text)
            elif is_final:
                # Accumulate without emitting — wait for speech_final or UtteranceEnd
                if self._pending and self._pending.lower() != text.lower():
                    self._pending = (self._pending + " " + text).strip()
                else:
                    self._pending = text
                self._arm_force_emit(self._pending)
 
        elif typ == "UtteranceEnd":
            # Deepgram confirmed end of utterance (after utterance_end_ms of silence).
            # Emit any accumulated pending fragment that hasn't fired yet.
            if self._pending:
                text = self._pending
                self._cancel_force_emit()
                self._pending = None
                logger.debug(f"UtteranceEnd — emitting pending: '{text[:60]}'")
                await self._emit(text)
 
        elif typ == "Error":
            logger.error(f"Deepgram error: {msg}")
 
    def _arm_force_emit(self, text: str) -> None:
        self._cancel_force_emit()
        # NEW: use adaptive timeout from profiler if available, else fall back to module default
        timeout = (
            self._get_force_emit_timeout()
            if self._get_force_emit_timeout
            else FORCE_EMIT_TIMEOUT
        )

        async def _timer():
            await asyncio.sleep(timeout)
            if self._pending == text:
                self._pending = None
                await self._emit(text)
 
        self._force_task = asyncio.create_task(_timer())
 
    def _cancel_force_emit(self) -> None:
        if self._force_task and not self._force_task.done():
            self._force_task.cancel()
        self._force_task = None
 
    async def _emit(self, text: str) -> None:
        if self._on_transcript:
            try:
                call_trace.log("STT", "transcript emitted", detail=f"'{text[:80]}'")
                await self._on_transcript(text)
            except Exception as e:
                logger.error(f"on_transcript callback error: {e}")
 
 
class DeepgramSTT:
    """
    Manages Deepgram WebSocket connection.

    Usage:
        async with stt.connect(on_transcript=my_async_fn) as conn:
            await conn.send_audio(mulaw_bytes)
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "nova-3",       # FIX: nova-2-general does NOT exist
        language: str = "",           # empty = auto-detect (detect_language=true)
        endpointing_ms: int = 150,
        **kwargs,
    ):
        self._api_key       = api_key or os.getenv("DEEPGRAM_API_KEY", "")
        self._model         = model or "nova-3"
        # Language resolution:
        #   "" / "auto"  → default (Hindi) — best for Hindi+English code-switched callers
        #   "multi"      → Nova-3 multi-lang auto-detect (RISKY: short Hindi can land as Spanish/French)
        #   "hi"/"en"/…  → pinned language
        lang = (language or "").strip().lower()
        self._language = "" if lang in ("", "auto") else lang
        self._endpointing_ms = endpointing_ms or 150

    def _build_url(self, endpointing_ms: Optional[int] = None) -> str:
        # NEW: accept an override endpointing_ms (from pace profiler); fall back to init value
        ep = endpointing_ms if endpointing_ms is not None else self._endpointing_ms
        url = (
            "wss://api.deepgram.com/v1/listen"
            f"?model={self._model}"
            "&encoding=linear16"
            "&sample_rate=8000"
            "&channels=1"
            f"&endpointing={ep}"
            "&interim_results=true"
            "&smart_format=false"
            "&no_delay=true"
            "&vad_events=true"
            "&utterance_end_ms=1000"   # was 1200 — 1000ms is Deepgram's minimum; still saves 200ms vs before
        )
        if self._language:
            # Pinned language (or explicit "multi") — use as-is.
            url += f"&language={self._language}"
        else:
            # Default: Hindi.  Nova-3 Hindi model transcribes Hindi+English
            # code-switched speech ("AC नहीं चल रहा मेरा") natively.  We do NOT
            # default to `multi` because short utterances can be misclassified
            # to languages this deployment never sees (Spanish, French, etc.).
            # Pure-English callers are handled by the runtime language swap in
            # streaming_pipeline._swap_deepgram after their first turn.
            url += "&language=hi"
        return url
 
    @asynccontextmanager
    async def connect(
        self,
        on_transcript: Callable[[str], Awaitable[None]],
        on_speech_start: Optional[Callable[[], Awaitable[None]]] = None,
        endpointing_ms: Optional[int] = None,                         # NEW: adaptive override
        get_force_emit_timeout: Optional[Callable[[], float]] = None, # NEW: adaptive override
    ):
        url     = self._build_url(endpointing_ms=endpointing_ms)  # NEW: pass adaptive ep
        headers = {"Authorization": f"Token {self._api_key}"}
        logger.info(f"Deepgram connecting: model={self._model} lang={self._language or 'hi (default)'}")
        breaker = CircuitBreakerRegistry.get("deepgram")
        t0 = time.monotonic()
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers=headers,
                    ping_interval=10,   # keep-alive every 10s — prevents mid-call drop
                    ping_timeout=20,
                ),
                timeout=10.0,  # fail fast if Deepgram is unreachable
            )
            if breaker:
                breaker.record_success()
        except Exception as e:
            if breaker:
                breaker.record_failure(e)
            raise
        conn_ms = (time.monotonic() - t0) * 1000
        call_trace.log("STT", "deepgram connected", duration_ms=conn_ms, detail=f"model={self._model} lang={self._language or 'auto'}")
        conn = DeepgramConnection(
            ws,
            on_transcript,
            on_speech_start,
            get_force_emit_timeout,   # NEW: pass adaptive timeout callable
        )
        conn._start_receive()
        try:
            yield conn
        finally:
            await conn.close()
            logger.info("Deepgram connection closed")
