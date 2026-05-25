from abc import ABC, abstractmethod


class BaseSTT(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> str:
        """
        Transcribe raw PCM audio (16-bit mono) to text.

        Args:
            audio: raw PCM bytes
            sample_rate: audio sample rate in Hz

        Returns:
            Transcribed text string
        """
        ...
