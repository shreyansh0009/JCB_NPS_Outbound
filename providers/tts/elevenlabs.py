"""
ElevenLabsTTS — ElevenLabs Flash v2.5, AudioSocket-compatible, multilingual Indian-language support.

Output : pcm_16000 (16-bit PCM 16 kHz) → resampled to 8 kHz slin16 for AudioSocket.
Model  : eleven_flash_v2_5  (~75 ms first-chunk latency)

Multilingual support
--------------------
ElevenLabs Flash v2.5 supports 32+ languages including major Indian languages.
Set per-language voice IDs via env vars (see .env.example) or the voice_language_map
constructor argument. When set_language() is called, the TTS switches to the
appropriate voice automatically.

Env vars
--------
ELEVENLABS_API_KEY              required
ELEVENLABS_VOICE_ID             default: 21m00Tcm4TlvDq8ikWAM  (Rachel — English)
ELEVENLABS_MODEL                default: eleven_flash_v2_5
ELEVENLABS_STABILITY            0.0-1.0  default 0.5
ELEVENLABS_SIMILARITY_BOOST     0.0-1.0  default 0.75
ELEVENLABS_STYLE                0.0-1.0  default 0.0

Per-language voice overrides (optional):
ELEVENLABS_VOICE_HI             Hindi voice ID
ELEVENLABS_VOICE_BN             Bengali voice ID
ELEVENLABS_VOICE_TE             Telugu voice ID
ELEVENLABS_VOICE_MR             Marathi voice ID
ELEVENLABS_VOICE_TA             Tamil voice ID
ELEVENLABS_VOICE_GU             Gujarati voice ID
ELEVENLABS_VOICE_KN             Kannada voice ID
ELEVENLABS_VOICE_PA             Punjabi voice ID
ELEVENLABS_VOICE_ML             Malayalam voice ID
ELEVENLABS_VOICE_OR             Odia voice ID
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, ClassVar, Optional

import audioop

import httpx

from core.call_logger import call_trace
from core.circuit_breaker import CircuitBreakerRegistry
from providers.tts.base import BaseTTS

logger = logging.getLogger(__name__)

_BASE = "https://api.elevenlabs.io/v1"

# ElevenLabs Flash v2.5 supports these Indian languages natively.
# Maps BCP-47-style lang codes → language name (informational).
SUPPORTED_INDIAN_LANGUAGES = {
    "hi": "Hindi",
    "bn": "Bengali",
    "te": "Telugu",
    "mr": "Marathi",
    "ta": "Tamil",
    "gu": "Gujarati",
    "kn": "Kannada",
    "pa": "Punjabi",
    "ml": "Malayalam",
    "or": "Odia",
}


class ElevenLabsTTS(BaseTTS):

    _shared_client: ClassVar[Optional[httpx.AsyncClient]] = None

    @classmethod
    def _get_client(cls) -> httpx.AsyncClient:
        """Return the shared AsyncClient, creating it on first call."""
        if cls._shared_client is None or cls._shared_client.is_closed:
            cls._shared_client = httpx.AsyncClient(timeout=30)
        return cls._shared_client

    def __init__(
        self,
        api_key: str,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model: str = "eleven_flash_v2_5",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        sample_rate: int = 8000,
        voice_language_map: Optional[dict[str, str]] = None,
        **_ignored,
    ):
        # Strip any accidental whitespace from voice_id (common .env copy-paste issue)
        self.api_key          = api_key.strip()
        self._default_voice   = voice_id.strip()
        self.voice_id         = self._default_voice   # active voice
        self.model            = model.strip()
        self.stability        = stability
        self.similarity_boost = similarity_boost
        self.style            = style
        # ElevenLabs doesn't support pcm_8000, so we get pcm_16000 and resample
        self._output_format   = "pcm_16000"
        self._target_rate     = sample_rate  # 8000 for AudioSocket
        # Map of lang_code -> voice_id for language-specific voices
        self._voice_map: dict[str, str] = voice_language_map or {}
        # FIX: audioop resample state — carried across chunks to prevent alignment cracks
        self._resample_state  = None

    # ── Resample 16kHz → 8kHz ─────────────────────────────────────────────

    def _resample_16k_to_8k(self, pcm_16k: bytes, state=None):
        """
        Downsample 16-bit PCM from 16kHz to 8kHz using audioop.
        Returns (resampled_bytes, new_state).
        When state is passed, it is carried across chunks — prevents
        alignment cracks at chunk boundaries.
        """
        if self._target_rate >= 16000:
            return pcm_16k, state
        resampled, new_state = audioop.ratecv(
            pcm_16k, 2, 1, 16000, self._target_rate, state
        )
        return resampled, new_state

    # ── Language switching ────────────────────────────────────────────────

    def set_language(self, lang_code: str) -> bool:
        """
        Switch the active voice for the given language code.
        Returns True if the voice actually changed.

        If no voice is configured for the language, the default voice is used —
        ElevenLabs Flash v2.5 handles multilingual output natively on any voice.
        """
        new_voice = self._voice_map.get(lang_code, self._default_voice)
        if new_voice == self.voice_id:
            return False
        old_voice = self.voice_id
        self.voice_id = new_voice
        logger.info(
            f"ElevenLabs voice switched for lang={lang_code}: "
            f"{old_voice} → {new_voice}"
        )
        return True

    def get_current_language_voice(self) -> str:
        return self.voice_id

    @property
    def _headers(self) -> dict:
        return {
            "xi-api-key":   self.api_key,
            "Content-Type": "application/json",
        }

    def _body(self, text: str, rate: float = 1.0) -> dict:
        return {
            "text":     text,
            "model_id": self.model,
            "voice_settings": {
                "stability":         self.stability,
                "similarity_boost":  self.similarity_boost,
                "style":             self.style,
                "use_speaker_boost": True,
            },
        }

    async def synthesize(self, text: str, rate: float = 1.0) -> bytes:
        """Non-streaming: returns complete PCM audio bytes (resampled to 8kHz)."""
        breaker = CircuitBreakerRegistry.get("elevenlabs")
        url = f"{_BASE}/text-to-speech/{self.voice_id}?output_format={self._output_format}"
        logger.info(f"ElevenLabs synthesize | voice={self.voice_id} | text='{text[:50]}'")
        try:
            client = self._get_client()
            resp = await client.post(url, headers=self._headers, json=self._body(text, rate=rate))
            if resp.status_code != 200:
                logger.error(f"ElevenLabs synthesize FAILED {resp.status_code}: {resp.text[:300]}")
                if breaker:
                    breaker.record_failure()
                resp.raise_for_status()
            # FIX: use stateful resample (state=None is fine for one-shot full audio)
            pcm, _ = self._resample_16k_to_8k(resp.content, state=None)
            logger.info(f"ElevenLabs synthesize OK — {len(pcm)} bytes (resampled to {self._target_rate}Hz)")
            if breaker:
                breaker.record_success()
            return pcm
        except Exception as e:
            if breaker:
                breaker.record_failure(e)
            raise

    async def stream_synthesize(self, text: str, language_code: Optional[str] = None, rate: float = 1.0):
        """
        Streaming: yields slin16 PCM 8kHz chunks (resampled from 16kHz).
        Pass language_code for better multilingual rendering (Indian languages).

        FIX: audioop resample state is carried across chunks so chunk boundaries
        don't produce alignment glitches / cracks.
        """
        # Reset resample state at the start of each new utterance
        self._resample_state = None

        url = (
            f"{_BASE}/text-to-speech/{self.voice_id}/stream"
            f"?output_format={self._output_format}"
        )
        logger.info(
            f"ElevenLabs stream | voice={self.voice_id} | model={self.model} "
            f"| lang={language_code} | text='{text[:50]}'"
        )
        breaker = CircuitBreakerRegistry.get("elevenlabs")
        total_bytes = 0
        t0 = time.monotonic()
        first_chunk_logged = False
        resample_state = None
        try:
            async with self._get_client().stream(
                "POST", url,
                headers=self._headers,
                json=self._body(text, rate=rate),
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    logger.error(
                        f"ElevenLabs stream FAILED {resp.status_code}: {err[:300]}\n"
                        f"URL was: {url}\n"
                        f"Voice ID: '{self.voice_id}'"
                    )
                    call_trace.log("TTS", "stream FAILED", duration_ms=(time.monotonic() - t0) * 1000, detail=f"status={resp.status_code}")
                    if breaker:
                        breaker.record_failure()
                    resp.raise_for_status()

                async for chunk in resp.aiter_bytes(chunk_size=1280):
                    if chunk:
                        if not first_chunk_logged:
                            first_chunk_ms = (time.monotonic() - t0) * 1000
                            call_trace.log("TTS", "first chunk (TTFB)", duration_ms=first_chunk_ms, detail=f"voice={self.voice_id}")
                            first_chunk_logged = True

                        # FIX: stateful resample — carry state across chunks
                        # This prevents crack/glitch at every 1280-byte chunk boundary
                        resampled, self._resample_state = self._resample_16k_to_8k(
                            chunk, state=self._resample_state
                        )
                        total_bytes += len(resampled)
                        yield resampled

            total_ms = (time.monotonic() - t0) * 1000
            call_trace.log("TTS", "stream complete", duration_ms=total_ms, detail=f"{total_bytes} bytes | text='{text[:40]}'")
            logger.info(f"ElevenLabs stream done — {total_bytes} bytes total (resampled)")
            if breaker:
                breaker.record_success()
        except Exception as e:
            if breaker:
                breaker.record_failure(e)
            raise

    @classmethod
    def from_env(cls) -> "ElevenLabsTTS":
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is not set in environment")

        # Build per-language voice map from env vars
        lang_env_map = {
            "hi": "ELEVENLABS_VOICE_HI",
            "bn": "ELEVENLABS_VOICE_BN",
            "te": "ELEVENLABS_VOICE_TE",
            "mr": "ELEVENLABS_VOICE_MR",
            "ta": "ELEVENLABS_VOICE_TA",
            "gu": "ELEVENLABS_VOICE_GU",
            "kn": "ELEVENLABS_VOICE_KN",
            "pa": "ELEVENLABS_VOICE_PA",
            "ml": "ELEVENLABS_VOICE_ML",
            "or": "ELEVENLABS_VOICE_OR",
        }
        voice_language_map = {
            lang: os.environ[env_var]
            for lang, env_var in lang_env_map.items()
            if os.environ.get(env_var, "").strip()
        }
        if voice_language_map:
            logger.info(f"Loaded per-language voice map: {list(voice_language_map.keys())}")

        return cls(
            api_key              = api_key,
            voice_id             = os.getenv("ELEVENLABS_VOICE_ID", "6V9kz8WiEZCuxIP4zw8F"),
            model                = os.getenv("ELEVENLABS_MODEL",    "eleven_flash_v2_5"),
            stability            = float(os.getenv("ELEVENLABS_STABILITY",        "0.35")),
            similarity_boost     = float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
            style                = float(os.getenv("ELEVENLABS_STYLE",            "0.40")),
            voice_language_map   = voice_language_map,
        )
