"""
GroqLLM: ultra-fast hosted inference via Groq.

Groq's LPU hardware delivers 300–500 tokens/sec — 10–20x faster than CPU Ollama.
Uses the OpenAI-compatible REST API (no extra SDK needed beyond httpx).

Recommended models (by use case):
  llama-3.3-70b-versatile  — best quality,  ~280 tok/s
  llama-3.1-8b-instant     — fastest,       ~750 tok/s  (use for simple agents)
  mixtral-8x7b-32768       — long context,  ~500 tok/s

Env vars:
  GROQ_API_KEY    - required
  GROQ_MODEL      - default: llama-3.3-70b-versatile
  GROQ_TIMEOUT    - default: 15
  LLM_TEMPERATURE - default: 0.25
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator, ClassVar, Optional

import time

import httpx

from core.call_logger import call_trace
from core.circuit_breaker import CircuitBreakerRegistry, CircuitOpenError
from core.tracing import set_attr as _otel_set_attr
from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqLLM(BaseLLM):
    # Shared AsyncClient — reuses TCP connections across all calls (connection pooling).
    # Pool sized via env to allow scaling with MAX_CONCURRENT_CALLS without code change.
    _shared_client: ClassVar[Optional[httpx.AsyncClient]] = None
    # Per-process semaphore — actual ceiling on simultaneous in-flight LLM requests.
    # The httpx pool is sized larger than this so the semaphore (not socket exhaustion)
    # is the visible backpressure point.
    _request_sem: ClassVar[Optional[asyncio.Semaphore]] = None

    @classmethod
    def _get_client(cls) -> httpx.AsyncClient:
        if cls._shared_client is None or cls._shared_client.is_closed:
            max_conn = int(os.getenv("HTTPX_MAX_CONNECTIONS", "600"))
            max_keepalive = int(os.getenv("HTTPX_MAX_KEEPALIVE", "200"))
            keepalive_expiry = int(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "30"))
            cls._shared_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=max_keepalive,
                    max_connections=max_conn,
                    keepalive_expiry=keepalive_expiry,
                ),
                http2=False,  # HTTP/1.1 keepalive is sufficient and avoids stream-id contention
            )
        return cls._shared_client

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        if cls._request_sem is None:
            # Default ceiling = MAX_CONCURRENT_CALLS so each call can have one in-flight LLM
            # request without queueing. Safe upper bound; Groq rate-limit handles the rest.
            ceiling = int(os.getenv("LLM_MAX_INFLIGHT", os.getenv("MAX_CONCURRENT_CALLS", "50")))
            cls._request_sem = asyncio.Semaphore(max(1, ceiling))
        return cls._request_sem

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        temperature: float = 0.25,
        top_p: float = 0.85,
        frequency_penalty: float = 0.35,
        presence_penalty: float = 0.05,
        timeout: int = 15,
        max_tokens: int = 120,  # base for English; doubled for Hindi/non-English
        fallback_model: str | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _get_max_tokens(self, messages: list[dict]) -> int:
        """
        Hindi/Indian scripts use ~2x tokens per word vs English.
        Cap non-English at 2x base (hard cap 400): a structured B2B question in
        Hindi can be 30+ words = 60-150 tokens; the old 250 cap cut replies mid-sentence.
        English stays at base (~100 words, ~20s speech max — enough for any single turn).

        Detection: Devanagari/Indian-script chars in system prompt (from persona.md
        Hindi examples), or an Indian-script assistant prefill injected for the turn.
        """
        import re
        _INDIAN_SCRIPT = re.compile(
            r'[ऀ-ॿ'   # Devanagari (Hindi, Marathi)
            r'ঀ-৿'   # Bengali
            r'਀-੿'   # Gurmukhi (Punjabi)
            r'஀-௿'   # Tamil
            r'ఀ-౿'   # Telugu
            r'ಀ-೿'   # Kannada
            r'ഀ-ൿ]'  # Malayalam
        )
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        # Devanagari in system prompt (persona.md always has Hindi examples)
        if _INDIAN_SCRIPT.search(system):
            return min(self.max_tokens * 2, 400)
        # Indian-script assistant prefill injected for this turn
        if any(
            m.get("role") == "assistant" and _INDIAN_SCRIPT.search(m.get("content", ""))
            for m in messages
        ):
            return min(self.max_tokens * 2, 400)
        return self.max_tokens  # English: base

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat with circuit breaker + automatic retry on 429."""
        import random
        breaker = CircuitBreakerRegistry.get("groq")
        max_retries = 4
        t0 = time.monotonic()

        # Fast-fail if circuit is open; try fallback model immediately
        if breaker and not breaker.allow_request():
            if self.fallback_model:
                logger.warning(f"[CircuitBreaker:groq] OPEN — using fallback model {self.fallback_model}")
                return await self._chat_with_model(self.fallback_model, messages, t0, label="fallback(cb)")
            raise CircuitOpenError("groq", breaker._recovery_timeout)

        async with self._get_semaphore():
            for attempt in range(max_retries):
                try:
                    client = self._get_client()
                    resp = await client.post(
                        f"{GROQ_BASE_URL}/chat/completions",
                        headers=self._headers,
                        timeout=self.timeout,
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "top_p": self.top_p,
                            "frequency_penalty": self.frequency_penalty,
                            "presence_penalty": self.presence_penalty,
                            "max_tokens": self._get_max_tokens(messages),
                            "stream": False,
                        },
                    )
                    if resp.status_code == 429:
                        # Honor Retry-After when present, otherwise exponential backoff with
                        # jitter to break up thundering herds when many calls hit the limit
                        # simultaneously.
                        base = float(resp.headers.get("retry-after", 2 ** attempt))
                        wait_s = min(base, 10) + random.uniform(0, 0.5 * (2 ** attempt))
                        logger.warning(f"Groq 429 rate limit — waiting {wait_s:.2f}s (attempt {attempt+1}/{max_retries})")
                        call_trace.log("LLM", "429 rate limit", detail=f"retry in {wait_s:.2f}s")
                        await asyncio.sleep(wait_s)
                        continue
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    dur_ms = (time.monotonic() - t0) * 1000
                    call_trace.log("LLM", "chat response", duration_ms=dur_ms, detail=f"model={self.model} chars={len(content)}")
                    if breaker:
                        breaker.record_success()
                    return content
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < max_retries - 1:
                        wait_s = (2 ** attempt) + random.uniform(0, 0.5 * (2 ** attempt))
                        logger.warning(f"Groq 429 (exception) — retrying in {wait_s:.2f}s")
                        await asyncio.sleep(wait_s)
                        continue
                    if breaker:
                        breaker.record_failure(e)
                    raise
                except Exception as e:
                    if breaker:
                        breaker.record_failure(e)
                    raise

        # Rate limit exhausted — try fallback model
        if self.fallback_model:
            logger.warning(f"Groq rate limit exhausted on {self.model} — trying fallback model {self.fallback_model}")
            call_trace.log("LLM", "fallback model attempt", detail=f"fallback={self.fallback_model}")
            return await self._chat_with_model(self.fallback_model, messages, t0, label="fallback(rl)")
        raise RuntimeError(f"Groq rate limit: failed after {max_retries} attempts")

    async def _chat_with_model(self, model: str, messages: list[dict], t0: float, label: str = "") -> str:
        """Internal: single non-streaming call with a specific model (no retry)."""
        client = self._get_client()
        resp = await client.post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers=self._headers,
            timeout=self.timeout,
            json={
                "model": model,
                "messages": messages,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "frequency_penalty": self.frequency_penalty,
                "presence_penalty": self.presence_penalty,
                "max_tokens": self._get_max_tokens(messages),
                "stream": False,
            },
        )
        if resp.status_code == 400:
            try:
                err_body = resp.text
            except Exception:
                err_body = "<unreadable>"
            msg_summary = [{"role": m.get("role"), "len": len(m.get("content", ""))} for m in messages]
            logger.error(f"Groq 400 — error_body={err_body!r} messages_structure={msg_summary}")
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        dur_ms = (time.monotonic() - t0) * 1000
        call_trace.log("LLM", f"chat response ({label})", duration_ms=dur_ms, detail=f"model={model} chars={len(content)}")
        return content

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """
        True streaming — yields text tokens as they arrive from Groq.
        Falls back to non-streaming on 429 or open circuit.
        """
        import random
        breaker = CircuitBreakerRegistry.get("groq")
        max_retries = 3

        # Fast-fail: if circuit open, fall back to non-streaming immediately
        if breaker and not breaker.allow_request():
            logger.warning("[CircuitBreaker:groq] OPEN — streaming fallback to non-stream")
            result = await self.chat(messages)
            yield result
            return

        async with self._get_semaphore():
            _otel_set_attr("llm.provider", "groq")
            _otel_set_attr("llm.model", self.model)
            for attempt in range(max_retries):
                try:
                    async with self._get_client().stream(
                        "POST",
                        f"{GROQ_BASE_URL}/chat/completions",
                        headers=self._headers,
                        timeout=self.timeout,
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "top_p": self.top_p,
                            "frequency_penalty": self.frequency_penalty,
                            "presence_penalty": self.presence_penalty,
                            "max_tokens": self._get_max_tokens(messages),
                            "stream": True,
                        },
                    ) as resp:
                        if resp.status_code == 429:
                            wait_s = min(2 ** attempt, 10) + random.uniform(0, 0.5 * (2 ** attempt))
                            logger.warning(f"Groq 429 on stream — waiting {wait_s:.2f}s")
                            await asyncio.sleep(wait_s)
                            continue
                        if resp.status_code != 200:
                            # Force-read body NOW while inside the streaming context.
                            # After raise_for_status(), e.response.text is unreadable
                            # on streaming responses because httpx has not buffered it.
                            await resp.aread()
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data == "[DONE]":
                                if breaker:
                                    breaker.record_success()
                                return
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0]["delta"].get("content", "")
                                if delta:
                                    yield delta
                            except (json.JSONDecodeError, KeyError):
                                continue
                        if breaker:
                            breaker.record_success()
                        return  # completed successfully
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 400:
                        # Log the full error body and the messages structure that caused it.
                        # This makes diagnosing future 400s trivial — look for role/content issues.
                        try:
                            err_body = e.response.text
                        except Exception:
                            err_body = "<unreadable>"
                        msg_summary = [
                            {"role": m.get("role"), "len": len(m.get("content", ""))}
                            for m in messages
                        ]
                        logger.error(
                            f"Groq 400 — error_body={err_body!r} "
                            f"messages_structure={msg_summary}"
                        )
                    if e.response.status_code == 429 and attempt < max_retries - 1:
                        wait_s = (2 ** attempt) + random.uniform(0, 0.5 * (2 ** attempt))
                        await asyncio.sleep(wait_s)
                        continue
                    if breaker:
                        breaker.record_failure(e)
                    raise
                except Exception as e:
                    if breaker:
                        breaker.record_failure(e)
                    raise

    @classmethod
    def from_env(cls) -> "GroqLLM":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is required")
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        fallback = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
        return cls(
            api_key=api_key,
            model=model,
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.25")),
            top_p=float(os.getenv("LLM_TOP_P", "0.85")),
            frequency_penalty=float(os.getenv("LLM_FREQUENCY_PENALTY", "0.35")),
            presence_penalty=float(os.getenv("LLM_PRESENCE_PENALTY", "0.05")),
            timeout=int(os.getenv("GROQ_TIMEOUT", os.getenv("LLM_TIMEOUT", "12"))),
            max_tokens=int(os.getenv("GROQ_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "200"))),
            fallback_model=fallback if fallback != model else None,
        )