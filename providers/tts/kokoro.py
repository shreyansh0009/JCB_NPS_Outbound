"""
KokoroTTS: alternative TTS via Kokoro (very natural-sounding, Apache 2.0 licensed).

Kokoro is a high-quality open-source TTS with multiple voices.
Better voice quality than Piper but slightly slower.

Install:
  pip install kokoro-onnx soundfile

Models:
  kokoro-v1.0.onnx  (available on Hugging Face: hexgrad/Kokoro-82M)

Env vars:
  KOKORO_MODEL_PATH  - path to .onnx model file
  KOKORO_VOICE       - voice name, default: af_heart (American female)
                       options: af_heart, af_bella, am_adam, bf_emma, bm_george
  KOKORO_SPEED       - speech speed, default: 1.0
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from functools import partial

import numpy as np

from providers.tts.base import BaseTTS

logger = logging.getLogger(__name__)


class KokoroTTS(BaseTTS):
    SAMPLE_RATE = 24000

    def __init__(
        self,
        model_path: str,
        voice: str = "af_heart",
        speed: float = 1.0,
    ):
        from kokoro_onnx import Kokoro
        logger.info(f"Loading Kokoro TTS model: {model_path}, voice={voice}")
        self._kokoro = Kokoro(model_path, "voices.bin")
        self._voice = voice
        self._speed = speed
        logger.info("Kokoro TTS model loaded")

    async def synthesize(self, text: str, rate: float = 1.0) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._synthesize_sync, text, rate))

    def _synthesize_sync(self, text: str, rate: float = 1.0) -> bytes:
        samples, _ = self._kokoro.create(text, voice=self._voice, speed=self._speed * rate, lang="en-us")
        # Convert float32 [-1,1] to int16 PCM
        pcm = (samples * 32767).astype(np.int16)
        return pcm.tobytes()

    @classmethod
    def from_env(cls) -> "KokoroTTS":
        model_path = os.environ.get("KOKORO_MODEL_PATH")
        if not model_path:
            raise ValueError("KOKORO_MODEL_PATH environment variable is required")
        return cls(
            model_path=model_path,
            voice=os.getenv("KOKORO_VOICE", "af_heart"),
            speed=float(os.getenv("KOKORO_SPEED", "1.0")),
        )
