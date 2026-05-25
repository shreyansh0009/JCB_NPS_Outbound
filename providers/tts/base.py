from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str, rate: float = 1.0) -> bytes: ...

    async def stream_synthesize(self, text: str, language_code: Optional[str] = None, rate: float = 1.0) -> AsyncIterator[bytes]:
        """Streaming TTS. Default wraps synthesize()."""
        yield await self.synthesize(text, rate=rate)

    def set_language(self, lang_code: str) -> bool:
        """Switch language. Returns True if changed."""
        return False

    @asynccontextmanager
    async def call_session(self):
        """Per-call persistent connection session. Default: returns self."""
        yield self
