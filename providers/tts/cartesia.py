"""
CartesiaTTS — Cartesia Sonic-3, AudioSocket-compatible, multilingual.
 
Output : pcm_s16le at 8000 Hz — slin16 PCM for Asterisk AudioSocket.
Model  : sonic-3  (industry-leading low latency, 42 languages)
 
Uses WebSocket streaming for lowest possible latency (real-time TTS).
Falls back to REST SSE for non-streaming synthesize().
 
Sonic-3 supports 42 languages including Hindi, Bengali, Tamil, Telugu etc.
Pass the `language` field in the API body for best results with non-English text.
 
Env vars
--------
CARTESIA_API_KEY        required
CARTESIA_VOICE_ID       default: a0e99841-438c-4a64-b679-ae501e7d6091
CARTESIA_MODEL          default: sonic-3
CARTESIA_SPEED          0.6-1.5  default 1.0
CARTESIA_VOLUME         0.5-2.0  default 1.0
"""
from __future__ import annotations
 
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import ClassVar, Optional
 
import httpx
 
from core.call_logger import call_trace
from core.circuit_breaker import CircuitBreakerRegistry
from providers.tts.base import BaseTTS
 
logger = logging.getLogger(__name__)
 
_BASE = "https://api.cartesia.ai"
_VERSION = "2026-03-01"
_DEFAULT_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"
 
# Map internal lang codes → Cartesia BCP-47 language codes.
# Sonic-3 supports Marathi natively, so we map mr→mr (was mr→hi historically
# when only sonic-2 was available — that fallback is no longer needed).
_LANG_MAP = {
    "hi": "hi",  "bn": "bn",  "te": "te",  "mr": "mr",  "ta": "ta",
    "gu": "gu",  "kn": "kn",  "pa": "pa",  "ml": "ml",  "or": "or",
    "en": "en",
}
 
 
class CartesiaTTS(BaseTTS):
    # Shared AsyncClient — reuses TCP connections for REST synthesize() and SSE streaming
    _shared_client: ClassVar[Optional[httpx.AsyncClient]] = None
 
    @classmethod
    def _get_client(cls) -> httpx.AsyncClient:
        if cls._shared_client is None or cls._shared_client.is_closed:
            max_conn = int(os.getenv("HTTPX_MAX_CONNECTIONS", "600"))
            max_keepalive = int(os.getenv("HTTPX_MAX_KEEPALIVE", "200"))
            keepalive_expiry = int(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "30"))
            cls._shared_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=max_keepalive,
                    max_connections=max_conn,
                    keepalive_expiry=keepalive_expiry,
                ),
            )
        return cls._shared_client
 
    def __init__(
        self,
        api_key: str,
        voice_id: str = _DEFAULT_VOICE,
        model: str = "sonic-3",
        sample_rate: int = 8000,
        speed: float = 1.1,
        volume: float = 1.0,
        **_ignored,
    ):
        self.api_key     = api_key.strip()
        self.voice_id    = voice_id.strip()
        self.model       = model.strip()
        self.sample_rate = sample_rate
        self.speed       = max(0.6, min(1.5, speed))
        self.volume      = max(0.5, min(2.0, volume))
        self._language   = "en"
        self._ws_client: Optional[object] = None
        self._call_ws: Optional[object] = None
        self._call_client: Optional[object] = None
 
    def set_language(self, lang_code: str) -> bool:
        mapped = _LANG_MAP.get(lang_code, "en")
        if mapped == self._language:
            return False
        old = self._language
        self._language = mapped
        logger.info(f"Cartesia language: {old} → {mapped}")
        return True
 
    @property
    def _headers(self) -> dict:
        return {
            "X-API-Key":        self.api_key,
            "Cartesia-Version": _VERSION,
            "Content-Type":     "application/json",
        }
 
    def _output_format(self) -> dict:
        return {
            "container":   "raw",
            "encoding":    "pcm_s16le",
            "sample_rate": self.sample_rate,
        }
 
    def _generation_config(self) -> dict:
        return {
            "speed":  self.speed,
            "volume": self.volume,
        }
 
    def _body(self, text: str) -> dict:
        return {
            "model_id":          self.model,
            "transcript":        text,
            "voice":             {"mode": "id", "id": self.voice_id},
            "language":          self._language,
            "output_format":     self._output_format(),
            "generation_config": self._generation_config(),
        }
 
    # ── Non-streaming (REST) ──────────────────────────────────────────
 
    async def synthesize(self, text: str) -> bytes:
        url = f"{_BASE}/tts/bytes"
        logger.info(f"Cartesia synthesize | voice={self.voice_id} | lang={self._language} | text='{text[:50]}'")
        breaker = CircuitBreakerRegistry.get("cartesia")
        try:
            client = self._get_client()
            resp = await client.post(url, headers=self._headers, json=self._body(text), timeout=20)
            if resp.status_code != 200:
                logger.error(f"Cartesia synthesize FAILED {resp.status_code}: {resp.text[:300]}")
                if breaker:
                    breaker.record_failure()
                resp.raise_for_status()
            logger.info(f"Cartesia synthesize OK — {len(resp.content)} bytes")
            if breaker:
                breaker.record_success()
            return resp.content
        except Exception as e:
            if breaker:
                breaker.record_failure(e)
            raise
 
    # ── Per-call persistent WebSocket session ─────────────────────────
 
    @asynccontextmanager
    async def call_session(self):
        """
        Open a persistent WebSocket for the duration of a call using the
        Cartesia SDK v3 API (websocket_connect → AsyncTTSResourceConnection).
 
        SDK v3 changed the WS API:
          OLD (v2/backcompat): tts.websocket() → AsyncBackcompatTTSResourceConnection
                               .context() accepts only (context_id) — no model_id
          NEW (v3):            tts.websocket_connect() → AsyncTTSResourceConnectionManager
                               async with ... as conn → AsyncTTSResourceConnection
                               conn.context(model_id=...) ← works correctly
        """
        client = None
        conn_mgr = None
        opened = False
        try:
            from cartesia import AsyncCartesia
            client = AsyncCartesia(api_key=self.api_key)
            # v3 API: websocket_connect() returns an async context manager
            conn_mgr = client.tts.websocket_connect()
            conn = await conn_mgr.__aenter__()   # → AsyncTTSResourceConnection
            self._call_ws = conn                 # conn.context(model_id=...) works ✓
            self._call_client = client
            opened = True
            logger.info("Cartesia persistent WS session opened (SDK v3)")
            call_trace.log("TTS", "call_session opened (persistent WS v3)")
        except ImportError:
            logger.warning("cartesia SDK not installed — call_session: per-request fallback")
        except Exception as e:
            logger.warning(f"Cartesia call_session setup failed ({e}) — per-request fallback")
            self._call_ws = None
 
        try:
            yield self
        finally:
            self._call_ws = None
            self._call_client = None
            if conn_mgr is not None and opened:
                try:
                    await conn_mgr.__aexit__(None, None, None)
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            if opened:
                logger.info("Cartesia persistent WS session closed")
 
    # ── WebSocket streaming (lowest latency) ──────────────────────────
 
    async def stream_synthesize(self, text: str, language_code: str | None = None):
        """
        Real-time streaming via WebSocket — yields PCM s16le chunks at 8kHz.
        Uses the Cartesia Python SDK for WebSocket connection management.
        Falls back to SSE if the SDK is not installed.
        """
        from core.tracing import set_attr as _otel_set_attr
        _otel_set_attr("tts.provider", "cartesia")
        _otel_set_attr("tts.model", self.model)
        _otel_set_attr("tts.language", self._language)
        _otel_set_attr("tts.text_len", len(text))
        if self._call_ws is not None:
            _otel_set_attr("tts.session", "ws-reuse")
            async for chunk in self._ws_stream_reuse(text):
                yield chunk
            return
        try:
            _otel_set_attr("tts.session", "ws-fresh")
            async for chunk in self._ws_stream(text):
                yield chunk
        except ImportError:
            logger.warning("cartesia SDK not installed, falling back to SSE streaming")
            _otel_set_attr("tts.session", "sse-fallback")
            async for chunk in self._sse_stream(text):
                yield chunk
 
    async def _ws_stream(self, text: str):
        """
        Stream via WebSocket using Cartesia SDK v3 API.
 
        v3 requires websocket_connect() (returns AsyncTTSResourceConnectionManager).
        The old websocket() method returns AsyncBackcompatTTSResourceConnection whose
        context() does NOT accept model_id — only the new AsyncTTSResourceConnection does.
        """
        from cartesia import AsyncCartesia
 
        t0 = time.monotonic()
        total_bytes = 0
        first_chunk_logged = False
 
        logger.info(
            f"Cartesia WS stream | voice={self.voice_id} | lang={self._language} "
            f"| model={self.model} | speed={self.speed} | volume={self.volume} "
            f"| text='{text[:50]}'"
        )
 
        client = AsyncCartesia(api_key=self.api_key)
        try:
            # v3 API: websocket_connect() → AsyncTTSResourceConnectionManager
            # async with → AsyncTTSResourceConnection (has context(model_id=...) ✓)
            async with client.tts.websocket_connect() as conn:
                ctx = conn.context(
                    model_id=self.model,
                    voice={"mode": "id", "id": self.voice_id},
                    output_format=self._output_format(),
                    language=self._language,
                    generation_config=self._generation_config(),
                )
                await ctx.push(text)
                await ctx.no_more_inputs()
 
                async for response in ctx.receive():
                    if response.type == "chunk" and response.audio:
                        if not first_chunk_logged:
                            first_chunk_ms = (time.monotonic() - t0) * 1000
                            call_trace.log("TTS", "first chunk (TTFB)", duration_ms=first_chunk_ms, detail=f"voice={self.voice_id} [WS]")
                            first_chunk_logged = True
                        total_bytes += len(response.audio)
                        yield response.audio
                    elif response.type == "done":
                        break
        finally:
            await client.close()
 
        total_ms = (time.monotonic() - t0) * 1000
        call_trace.log("TTS", "stream complete", duration_ms=total_ms, detail=f"{total_bytes} bytes [WS] | text='{text[:40]}'")
        logger.info(f"Cartesia WS stream done — {total_bytes} bytes total")
 
    async def _ws_stream_reuse(self, text: str):
        """Stream via the persistent per-call WebSocket (no client/ws creation overhead).
 
        IMPORTANT: Each context creates an entry in the SDK's internal
        _context_queues dict.  If we exit without receiving a "done" event
        (e.g. barge-in cancellation), we MUST call ctx.cancel() so the SDK
        pops the queue.  Otherwise orphaned queues accumulate and the
        background _process_responses task slows down — causing progressive
        audio degradation over the life of the call.
        """
        t0 = time.monotonic()
        total_bytes = 0
        first_chunk_logged = False
        ctx = self._call_ws.context(
            model_id=self.model,
            voice={"mode": "id", "id": self.voice_id},
            output_format=self._output_format(),
            language=self._language,
            generation_config=self._generation_config(),
        )
        completed_normally = False
        try:
            await ctx.push(text)
            await ctx.no_more_inputs()
            async for response in ctx.receive():
                if response.type == "chunk" and response.audio:
                    if not first_chunk_logged:
                        call_trace.log("TTS", "first chunk (TTFB)", duration_ms=(time.monotonic()-t0)*1000, detail=f"voice={self.voice_id} [WS-reuse]")
                        first_chunk_logged = True
                    total_bytes += len(response.audio)
                    yield response.audio
                elif response.type == "done":
                    completed_normally = True
                    break
        finally:
            if not completed_normally:
                try:
                    await ctx.cancel()
                except Exception:
                    pass
                logger.info("Cartesia context cancelled (incomplete)")
        call_trace.log("TTS", "stream complete", duration_ms=(time.monotonic()-t0)*1000, detail=f"{total_bytes} bytes [WS-reuse] | text='{text[:40]}'")
 
    async def _sse_stream(self, text: str):
        """Fallback: streaming via SSE — yields PCM chunks as they arrive."""
        url = f"{_BASE}/tts/sse"
        t0 = time.monotonic()
        total_bytes = 0
        first_chunk_logged = False
 
        logger.info(
            f"Cartesia SSE stream | voice={self.voice_id} | lang={self._language} "
            f"| model={self.model} | text='{text[:50]}'"
        )
 
        breaker = CircuitBreakerRegistry.get("cartesia")
        try:
            async with self._get_client().stream(
                "POST", url,
                headers={**self._headers, "Accept": "text/event-stream"},
                json=self._body(text),
                timeout=30,
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    logger.error(f"Cartesia SSE stream FAILED {resp.status_code}: {err[:300]}")
                    call_trace.log("TTS", "stream FAILED", duration_ms=(time.monotonic() - t0) * 1000, detail=f"status={resp.status_code}")
                    if breaker:
                        breaker.record_failure()
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except Exception:
                        continue
                    if event.get("type") == "done" or event.get("done"):
                        break
                    chunk_b64 = event.get("data")
                    if chunk_b64:
                        chunk = base64.b64decode(chunk_b64)
                        if not first_chunk_logged:
                            first_chunk_ms = (time.monotonic() - t0) * 1000
                            call_trace.log("TTS", "first chunk (TTFB)", duration_ms=first_chunk_ms, detail=f"voice={self.voice_id} [SSE]")
                            first_chunk_logged = True
                        total_bytes += len(chunk)
                        yield chunk
            if breaker:
                breaker.record_success()
        except Exception as e:
            if breaker:
                breaker.record_failure(e)
            raise
 
        total_ms = (time.monotonic() - t0) * 1000
        call_trace.log("TTS", "stream complete", duration_ms=total_ms, detail=f"{total_bytes} bytes [SSE] | text='{text[:40]}'")
        logger.info(f"Cartesia SSE stream done — {total_bytes} bytes total")
 
 