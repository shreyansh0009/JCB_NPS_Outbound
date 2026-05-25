"""
Sprint 3: Response cache — three-tier Redis-backed cache.

Architecture:
  Primary backend : Redis (shared across all workers, required for multi-pod)
  Fallback        : in-memory LRU dict (single-pod, evicted at _MAX_LOCAL_ENTRIES)

Tiers:
  Tier 1  TTS audio   TTL=24h   key=tts:{voice_id}:{sha256(text)[:16]}
  Tier 2  LLM text    TTL=1h    key=llm:{agent}:{lang}:{sha256(norm_msg)[:16]}
  Tier 3  RAG context TTL=30m   key=rag:{agent}:{sha256(norm_query)[:16]}

TTS bytes are stored as base64 in Redis (JSON-safe).

Cache bypass:
  - LLM cache is skipped for agents with MCP tools (responses use live data).
  - Short LLM responses (< 5 chars) are not cached (likely error/empty).
  - Very long LLM responses (> 2000 chars) are not cached.

Usage:
    from core.response_cache import init_cache, get_cache

    # at startup:
    init_cache(redis_client=redis_client)   # or None for local-only

    # in agent:
    cache = get_cache()
    if cache:
        hit = await cache.get_llm(agent_name, language, user_message)
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_LOCAL_ENTRIES = 512   # per-pod LRU cap (TTS + LLM + RAG combined)

# Agents whose responses depend on per-call live state (CRM lookups, MCP tools,
# screener decisions, mid-call handoffs). NEVER cache these. Add new agents
# here when they are introduced.
_STATEFUL_LLM_AGENTS = frozenset({
    "service",     # MCP tools (slot lookup, booking)
    "sales",       # MCP tools (inventory, test drive booking)
    "scheduler",   # MCP tools (appointment scheduling)
    "screener",    # decides handoff path based on conversation state
    "closer",      # summarises live call context
})


def _llm_cache_enabled() -> bool:
    """Master switch — LLM caching is OFF by default. Opt-in via env."""
    return os.getenv("LLM_CACHE_ENABLED", "false").lower() in ("1", "true", "yes")


# ── in-memory LRU with per-entry TTL ─────────────────────────────────────────

class _LRUCache:
    """Minimal in-memory LRU with per-entry TTL. GIL-safe for asyncio."""

    def __init__(self, max_size: int = _MAX_LOCAL_ENTRIES):
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max = max_size

    def get(self, key: str) -> Optional[str]:
        if key not in self._data:
            return None
        val, expires = self._data[key]
        if time.monotonic() > expires:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return val

    def set(self, key: str, value: str, ttl: int) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, time.monotonic() + ttl)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


# ── helpers ───────────────────────────────────────────────────────────────────

def _sha(text: str) -> str:
    """16-char hex digest — collision probability negligible for our key space."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ── main class ────────────────────────────────────────────────────────────────

class ResponseCache:
    """
    Three-tier response cache. Create once at startup, share across all calls.

    All methods are async-safe. Redis errors are silently swallowed — the
    cache degrades to local-only without affecting call quality.
    """

    _TTS_TTL = 86_400   # 24 h — voice audio for a given text rarely changes
    _LLM_TTL =  3_600   # 1 h  — LLM replies are deterministic enough at temp=0.1
    _RAG_TTL =  1_800   # 30 m — knowledge base rarely changes during business hours

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._local = _LRUCache()
        mode = "Redis+local" if redis_client else "local-only"
        logger.info(f"[Cache] ResponseCache initialised ({mode})")

    # ── Redis I/O (silently fault-tolerant) ───────────────────────────────────

    async def _rget(self, key: str) -> Optional[str]:
        if not self._redis:
            return None
        try:
            val = await self._redis.get(key)
            if val is None:
                return None
            return val.decode() if isinstance(val, bytes) else val
        except Exception as exc:
            logger.debug(f"[Cache] Redis GET {key!r}: {exc}")
            return None

    async def _rset(self, key: str, value: str, ttl: int) -> None:
        if not self._redis:
            return
        try:
            await self._redis.setex(key, ttl, value)
        except Exception as exc:
            logger.debug(f"[Cache] Redis SET {key!r}: {exc}")

    # ── TTS audio (stored as base64) ──────────────────────────────────────────

    def _tts_key(self, voice_id: str, text: str) -> str:
        return f"tts:{voice_id}:{_sha(text)}"

    async def get_tts(self, voice_id: str, text: str) -> Optional[bytes]:
        key = self._tts_key(voice_id, text)
        # 1. local
        local = self._local.get(key)
        if local is not None:
            logger.debug(f"[Cache] TTS hit (local) voice={voice_id[:8]} chars={len(text)}")
            return base64.b64decode(local)
        # 2. Redis
        val = await self._rget(key)
        if val:
            audio = base64.b64decode(val)
            self._local.set(key, val, self._TTS_TTL)
            logger.debug(f"[Cache] TTS hit (Redis) voice={voice_id[:8]} bytes={len(audio)}")
            return audio
        return None

    async def set_tts(self, voice_id: str, text: str, audio: bytes) -> None:
        if not audio:
            return
        key   = self._tts_key(voice_id, text)
        b64   = base64.b64encode(audio).decode()
        self._local.set(key, b64, self._TTS_TTL)
        await self._rset(key, b64, self._TTS_TTL)
        logger.debug(f"[Cache] TTS stored voice={voice_id[:8]} bytes={len(audio)}")

    # ── LLM text ──────────────────────────────────────────────────────────────

    def _llm_key(self, agent: str, language: str, message: str) -> str:
        norm = " ".join(message.lower().split())
        return f"llm:{agent}:{language}:{_sha(norm)}"

    async def get_llm(self, agent: str, language: str, message: str) -> Optional[str]:
        # Two gates: master switch + per-agent denylist (live data agents must
        # never be cached or they'll return stale CRM/inventory/booking info).
        if not _llm_cache_enabled() or agent in _STATEFUL_LLM_AGENTS:
            return None

        key = self._llm_key(agent, language, message)
        local = self._local.get(key)
        if local is not None:
            logger.debug(f"[Cache] LLM hit (local) agent={agent} lang={language}")
            return local
        val = await self._rget(key)
        if val:
            self._local.set(key, val, self._LLM_TTL)
            logger.debug(f"[Cache] LLM hit (Redis) agent={agent} lang={language}")
            return val
        return None

    async def set_llm(
        self, agent: str, language: str, message: str, reply: str
    ) -> None:
        if not _llm_cache_enabled() or agent in _STATEFUL_LLM_AGENTS:
            return

        if not reply or len(reply) < 5 or len(reply) > 2000:
            return
        key = self._llm_key(agent, language, message)
        self._local.set(key, reply, self._LLM_TTL)
        await self._rset(key, reply, self._LLM_TTL)
        logger.debug(f"[Cache] LLM stored agent={agent} lang={language} len={len(reply)}")

    # ── RAG context ───────────────────────────────────────────────────────────

    def _rag_key(self, agent: str, query: str) -> str:
        norm = " ".join(query.lower().split())
        return f"rag:{agent}:{_sha(norm)}"

    async def get_rag(self, agent: str, query: str) -> Optional[str]:
        key = self._rag_key(agent, query)
        local = self._local.get(key)
        if local is not None:
            logger.debug(f"[Cache] RAG hit (local) agent={agent}")
            return local
        val = await self._rget(key)
        if val:
            self._local.set(key, val, self._RAG_TTL)
            logger.debug(f"[Cache] RAG hit (Redis) agent={agent}")
            return val
        return None

    async def set_rag(self, agent: str, query: str, context: str) -> None:
        if not context:
            return
        key = self._rag_key(agent, query)
        self._local.set(key, context, self._RAG_TTL)
        await self._rset(key, context, self._RAG_TTL)
        logger.debug(f"[Cache] RAG stored agent={agent} len={len(context)}")


# ── module-level singleton ────────────────────────────────────────────────────

_cache: Optional[ResponseCache] = None


def init_cache(redis_client=None) -> ResponseCache:
    """
    Create (or replace) the module-level ResponseCache singleton.
    Call once at startup, before any request handling.
    """
    global _cache
    _cache = ResponseCache(redis_client=redis_client)
    return _cache


def get_cache() -> Optional[ResponseCache]:
    """Return the active ResponseCache, or None if not initialised."""
    return _cache
