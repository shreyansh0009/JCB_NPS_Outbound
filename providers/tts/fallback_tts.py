"""
FallbackTTS — automatic failover between TTS providers.

Chain: ElevenLabs (primary) → Cartesia (secondary)

When the primary circuit breaker is OPEN or a request fails, FallbackTTS
switches to the secondary provider transparently — the caller never sees
the failure. Both providers implement the same BaseTTS interface.

Why this matters:
  ElevenLabs has ~99.9% uptime but occasional degradation events. At 5K
  concurrent calls, even a 60-second ElevenLabs outage affects thousands
  of active calls. FallbackTTS + circuit breakers cut the impact to <1s
  per call (the time to detect failure and switch to Cartesia).

Usage (in main.py):
    tts = FallbackTTS(
        primary=ElevenLabsTTS(...),
        secondary=CartesiaTTS(...),
    )
    pipeline = StreamingPipeline(tts=tts, ...)
"""
from __future__ import annotations

import logging
from typing import Optional

from core.circuit_breaker import CircuitBreakerRegistry, CircuitOpenError
from providers.tts.base import BaseTTS

logger = logging.getLogger(__name__)


class FallbackTTS(BaseTTS):
    """
    Two-provider TTS with circuit-breaker-driven automatic failover.

    - primary   : First choice (e.g., ElevenLabs Flash v2.5)
    - secondary : Fallback (e.g., Cartesia Sonic-3)

    Circuit breaker names must match what is registered in CircuitBreakerRegistry:
      primary_cb_name   = "elevenlabs"
      secondary_cb_name = "cartesia"
    """

    def __init__(
        self,
        primary: BaseTTS,
        secondary: BaseTTS,
        primary_cb_name:   str = "elevenlabs",
        secondary_cb_name: str = "cartesia",
    ) -> None:
        self._primary   = primary
        self._secondary = secondary
        self._primary_cb_name   = primary_cb_name
        self._secondary_cb_name = secondary_cb_name
        self._active: str = "primary"   # for logging

    # ── Language routing (both providers must mirror each other) ─────────────

    def set_language(self, lang_code: str) -> bool:
        changed = False
        try:
            changed = self._primary.set_language(lang_code)
        except Exception:
            pass
        try:
            self._secondary.set_language(lang_code)
        except Exception:
            pass
        return changed

    # ── Synthesize (non-streaming) ────────────────────────────────────────────

    async def synthesize(self, text: str, rate: float = 1.0) -> bytes:
        primary_cb = CircuitBreakerRegistry.get(self._primary_cb_name)

        # Try primary if circuit allows
        if primary_cb is None or primary_cb.allow_request():
            try:
                result = await self._primary.synthesize(text, rate=rate)
                self._log_provider("synthesize", "primary")
                return result
            except CircuitOpenError:
                pass
            except Exception as e:
                logger.warning(
                    f"[FallbackTTS] Primary synthesize failed ({type(e).__name__}: {e}) "
                    "— switching to secondary"
                )

        # Fall through to secondary
        secondary_cb = CircuitBreakerRegistry.get(self._secondary_cb_name)
        if secondary_cb and not secondary_cb.allow_request():
            raise RuntimeError("[FallbackTTS] Both primary and secondary circuits are OPEN — TTS unavailable")

        try:
            result = await self._secondary.synthesize(text, rate=rate)
            self._log_provider("synthesize", "secondary")
            return result
        except Exception as e:
            logger.error(f"[FallbackTTS] Secondary synthesize also failed: {e}")
            raise

    # ── Stream synthesize ─────────────────────────────────────────────────────

    async def stream_synthesize(self, text: str, language_code: Optional[str] = None, rate: float = 1.0):
        """
        Streaming TTS with fallback. Yields PCM chunks.

        Strategy: attempt primary streaming. On first failure, switch to
        secondary for the remainder. We cannot "resume" mid-stream so on
        primary failure we start secondary from scratch.
        """
        primary_cb = CircuitBreakerRegistry.get(self._primary_cb_name)

        # Try primary if circuit allows
        if primary_cb is None or primary_cb.allow_request():
            try:
                yielded_any = False
                async for chunk in self._primary.stream_synthesize(text, language_code=language_code, rate=rate):
                    yielded_any = True
                    yield chunk
                if yielded_any:
                    self._log_provider("stream_synthesize", "primary")
                    return
            except CircuitOpenError:
                pass
            except Exception as e:
                logger.warning(
                    f"[FallbackTTS] Primary stream failed ({type(e).__name__}: {e}) "
                    "— switching to secondary"
                )

        # Fall through to secondary
        secondary_cb = CircuitBreakerRegistry.get(self._secondary_cb_name)
        if secondary_cb and not secondary_cb.allow_request():
            raise RuntimeError("[FallbackTTS] Both primary and secondary circuits are OPEN — TTS unavailable")

        try:
            async for chunk in self._secondary.stream_synthesize(text, language_code=language_code, rate=rate):
                yield chunk
            self._log_provider("stream_synthesize", "secondary")
        except Exception as e:
            logger.error(f"[FallbackTTS] Secondary stream also failed: {e}")
            raise

    # ── Health ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        primary_cb  = CircuitBreakerRegistry.get(self._primary_cb_name)
        secondary_cb = CircuitBreakerRegistry.get(self._secondary_cb_name)
        return {
            "primary":   primary_cb.get_status()  if primary_cb  else {"state": "unknown"},
            "secondary": secondary_cb.get_status() if secondary_cb else {"state": "unknown"},
        }

    def _log_provider(self, method: str, provider: str) -> None:
        if provider != self._active:
            if provider == "primary":
                logger.info(f"[FallbackTTS] {method} — switched BACK to primary")
            else:
                logger.warning(f"[FallbackTTS] {method} — using SECONDARY (primary unavailable)")
            self._active = provider
