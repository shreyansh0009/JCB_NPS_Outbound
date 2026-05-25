"""
OllamaLLM: calls a locally-hosted Ollama server.

Setup:
  # On EC2 or local:
  curl https://ollama.ai/install.sh | sh
  ollama pull llama3.2        # or mistral, qwen2.5, etc.
  ollama serve

Env vars:
  OLLAMA_BASE_URL  - default: http://localhost:11434
  OLLAMA_MODEL     - default: llama3.2
  OLLAMA_TIMEOUT   - default: 30 (seconds)
"""
from __future__ import annotations

import logging

import httpx

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)


class OllamaLLM(BaseLLM):
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2",
        timeout: int = 30,
        temperature: float = 0.25,
        top_p: float = 0.85,
        repeat_penalty: float = 1.08,
        max_tokens: int = 90,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.repeat_penalty = repeat_penalty
        self.max_tokens = max_tokens

    async def chat(self, messages: list[dict]) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "repeat_penalty": self.repeat_penalty,
                "num_predict": self.max_tokens,
            },
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]

    @classmethod
    def from_env(cls) -> "OllamaLLM":
        import os
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_MODEL", "llama3.2"),
            timeout=int(os.getenv("OLLAMA_TIMEOUT", os.getenv("LLM_TIMEOUT", "12"))),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.25")),
            top_p=float(os.getenv("LLM_TOP_P", "0.85")),
            repeat_penalty=float(os.getenv("LLM_REPEAT_PENALTY", "1.08")),
            max_tokens=int(os.getenv("OLLAMA_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "90"))),
        )
