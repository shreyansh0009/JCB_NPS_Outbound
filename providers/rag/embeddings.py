"""
Embeddings providers for RAG.

Options:
  1. OllamaEmbeddings  — uses Ollama /api/embed
  2. SentenceTransformerEmbeddings — local model via sentence-transformers
  3. HashEmbeddings — dependency-free deterministic fallback (works everywhere)

Env vars:
  EMBEDDING_PROVIDER   - "ollama" | "sentence_transformers" | "hash"
  OLLAMA_EMBED_MODEL   - default: nomic-embed-text
  EMBED_MODEL_NAME     - default: all-MiniLM-L6-v2 (for sentence_transformers)
  HASH_EMBED_DIM       - default: 384
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from abc import ABC, abstractmethod
from functools import partial

import httpx

logger = logging.getLogger(__name__)


class BaseEmbeddings(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Returns list of float vectors."""
        ...

    async def embed_one(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]


class OllamaEmbeddings(BaseEmbeddings):
    """
    Uses Ollama's /api/embed endpoint.
    Pull the model first: ollama pull nomic-embed-text
    """
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/api/embed"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json={"model": self.model, "input": texts})
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]

    @classmethod
    def from_env(cls) -> "OllamaEmbeddings":
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )


class SentenceTransformerEmbeddings(BaseEmbeddings):
    """
    Uses sentence-transformers (runs fully locally, no Ollama needed).
    Install: pip install sentence-transformers
    """
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading SentenceTransformer model: {model_name}")
        self._model = SentenceTransformer(model_name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        vectors = await loop.run_in_executor(
            None, partial(self._model.encode, texts, convert_to_numpy=True)
        )
        return [v.tolist() for v in vectors]

    @classmethod
    def from_env(cls) -> "SentenceTransformerEmbeddings":
        return cls(model_name=os.getenv("EMBED_MODEL_NAME", "all-MiniLM-L6-v2"))


class HashEmbeddings(BaseEmbeddings):
    """
    Lightweight deterministic embeddings with no external ML dependencies.
    Useful when torch/sentence-transformers wheels are unavailable (e.g. Py3.14).
    """
    def __init__(self, dim: int = 384):
        self.dim = max(64, int(dim))

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"\w+", (text or "").lower())
        if not tokens:
            return vec

        for tok in tokens:
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=16).digest()
            idx = int.from_bytes(digest[:8], "big") % self.dim
            sign = -1.0 if digest[8] & 1 else 1.0
            vec[idx] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]

    @classmethod
    def from_env(cls) -> "HashEmbeddings":
        return cls(dim=int(os.getenv("HASH_EMBED_DIM", "384")))


def get_embeddings() -> BaseEmbeddings:
    provider = os.getenv("EMBEDDING_PROVIDER", "sentence_transformers").strip().lower()
    if provider == "ollama":
        return OllamaEmbeddings.from_env()
    if provider in {"hash", "local_hash", "fallback"}:
        logger.warning("Using HashEmbeddings (dependency-free fallback)")
        return HashEmbeddings.from_env()
    if provider in {"sentence_transformers", "sentence-transformers", "st"}:
        try:
            return SentenceTransformerEmbeddings.from_env()
        except ModuleNotFoundError:
            logger.warning(
                "sentence_transformers not installed (or torch unavailable). "
                "Falling back to HashEmbeddings."
            )
            return HashEmbeddings.from_env()
    logger.warning(f"Unknown EMBEDDING_PROVIDER='{provider}' — using HashEmbeddings")
    return HashEmbeddings.from_env()
