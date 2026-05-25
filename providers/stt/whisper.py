"""
WhisperSTT: transcription via faster-whisper (CTranslate2-optimised Whisper).

Much faster than openai-whisper. Runs well on EC2 CPU instances.
GPU: use compute_type="float16", device="cuda"
CPU: use compute_type="int8",   device="cpu"

Install: pip install faster-whisper

Models (auto-downloaded on first use):
  "tiny"   - 39M params,  fastest,  least accurate
  "base"   - 74M params,  fast,     decent accuracy
  "small"  - 244M params, balanced  ← good default for voice agents
  "medium" - 769M params, slower,   good accuracy
  "large-v3" - 1.5B params, slowest, best accuracy

Env vars:
  WHISPER_MODEL      - default: small
  WHISPER_DEVICE     - default: cpu
  WHISPER_COMPUTE    - default: int8
  WHISPER_LANGUAGE   - default: en  (set to None for auto-detect)
"""
from __future__ import annotations

import io
import logging
import os
import asyncio
from functools import partial

import numpy as np

from providers.stt.base import BaseSTT

logger = logging.getLogger(__name__)


class WhisperSTT(BaseSTT):
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,  # None = auto-detect language
    ):
        from faster_whisper import WhisperModel
        logger.info(f"Loading Whisper model={model_size} device={device} compute={compute_type}")
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = language
        logger.info("Whisper model loaded")

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> str:
        # faster-whisper is synchronous — run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._transcribe_sync, audio, sample_rate))

    def _transcribe_sync(self, audio: bytes, sample_rate: int) -> str:
        # Convert raw PCM bytes -> float32 numpy array in [-1, 1]
        audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        # faster-whisper expects 16kHz; resample if needed
        if sample_rate != 16000:
            audio_np = self._resample(audio_np, sample_rate, 16000)

        segments, _ = self._model.transcribe(
            audio_np,
            language=self._language,
            beam_size=5,
            vad_filter=True,  # built-in VAD to skip silence
        )
        text = " ".join(seg.text.strip() for seg in segments)
        return text.strip()

    @staticmethod
    def _resample(audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
        try:
            import resampy
            return resampy.resample(audio, orig_rate, target_rate)
        except ImportError:
            # Fallback: linear interpolation (lower quality but no extra dep)
            ratio = target_rate / orig_rate
            new_len = int(len(audio) * ratio)
            return np.interp(
                np.linspace(0, len(audio) - 1, new_len),
                np.arange(len(audio)),
                audio,
            )

    @classmethod
    def from_env(cls) -> "WhisperSTT":
        return cls(
            model_size=os.getenv("WHISPER_MODEL", "small"),
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
            language=os.getenv("WHISPER_LANGUAGE") or None,  # None = auto-detect
        )
