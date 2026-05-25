"""
LiteLLMProvider: swap-in replacement that works with any model via LiteLLM.

This gives you access to:
  - Any Ollama model: "ollama/llama3.2", "ollama/mistral"
  - OpenAI-compatible APIs: "openai/gpt-4o"
  - AWS Bedrock, Anthropic, Groq, Together AI, etc.

Install: pip install litellm

Env vars for Ollama:
  LITELLM_MODEL=ollama/llama3.2
  OLLAMA_API_BASE=http://localhost:11434
"""
from __future__ import annotations

import os
import logging

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)


class LiteLLMProvider(BaseLLM):
    def __init__(self, model: str = "ollama/llama3.2", **kwargs):
        self.model = model
        self.kwargs = kwargs

    async def chat(self, messages: list[dict]) -> str:
        try:
            import litellm
        except ImportError:
            raise ImportError("Install litellm: pip install litellm")

        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            **self.kwargs,
        )
        return response.choices[0].message.content

    @classmethod
    def from_env(cls) -> "LiteLLMProvider":
        return cls(
            model=os.getenv("LITELLM_MODEL", "ollama/llama3.2"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.25")),
            top_p=float(os.getenv("LLM_TOP_P", "0.85")),
            frequency_penalty=float(os.getenv("LLM_FREQUENCY_PENALTY", "0.35")),
            presence_penalty=float(os.getenv("LLM_PRESENCE_PENALTY", "0.05")),
            repetition_penalty=float(os.getenv("LLM_REPEAT_PENALTY", "1.08")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "90")),
            timeout=int(os.getenv("LLM_TIMEOUT", "12")),
        )
