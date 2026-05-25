from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict]) -> str:
        """Send messages, return full reply text."""
        ...

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """
        Stream reply token-by-token.
        Default falls back to chat() — override in providers that support streaming.
        """
        reply = await self.chat(messages)
        yield reply
