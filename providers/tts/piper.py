"""
PiperTTS: fast, offline text-to-speech via Piper.

Piper is extremely fast on CPU — sub-100ms latency for short sentences.
Great for EC2 without GPU.

Install:
  pip install piper-tts

Or download the binary:
  https://github.com/rhasspy/piper/releases

Models (download from Hugging Face):
  en_US-lessac-medium   ← good default female voice
  en_US-ryan-high       ← good male voice
  en_GB-alan-medium

Usage:
  # Download a model (example):
  mkdir -p models/piper
  wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
  wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

Env vars:
  PIPER_MODEL_PATH   - path to .onnx model file (required)
  PIPER_SAMPLE_RATE  - default: 22050
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import partial

from providers.tts.base import BaseTTS

logger = logging.getLogger(__name__)


class PiperTTS(BaseTTS):
    def __init__(self, model_path: str, sample_rate: int = 22050):
        from piper import PiperVoice
        logger.info(f"Loading Piper TTS model: {model_path}")
        self._voice = PiperVoice.load(model_path)
        self.sample_rate = sample_rate
        logger.info("Piper TTS model loaded")

    async def synthesize(self, text: strep
        return await loop.run_in_e
    def _synthesize_sync(self, text: str, rate: float = 1.0) -> bytes:


        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            self._voice.synthesize(text, wav_file)
        buf.seek(0)

        # Return raw PCM (skip 44-byte WAV header)
        wav_bytes = buf.read()
        return wav_bytes[44:]  # strip WAV header, return raw PCM

    @classmethod
    def from_env(cls) -> "PiperTTS":
        model_path = os.environ.get("PIPER_MODEL_PATH")
        if not model_path:
            raise ValueError("PIPER_MODEL_PATH environment variable is required")
        return cls(
            model_path=model_path,
            sample_rate=int(os.getenv("PIPER_SAMPLE_RATE", "22050")),
        )
