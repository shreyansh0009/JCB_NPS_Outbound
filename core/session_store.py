"""
SessionStore — Redis-backed session persistence for horizontal scaling.

Why this exists:
  At 5K concurrent calls across multiple worker pods, a call can be routed to
  any pod by HAProxy. Without a shared session store, the second pod has no
  knowledge of the call's current agent, conversation history, or detected
  language. Every call drop and reconnect would restart from the hello agent.

Architecture:
  RedisSessionStore  → Primary. Uses redis asyncio client.
  InMemorySessionStore → Fallback when Redis is unavailable (single-pod only).

TTL: sessions expire after max_call_duration_s (default 600s = 10 minutes).
     This prevents stale sessions from accumulating in Redis.

Usage:
    store = await SessionStore.create(settings)

    # Save after every agent handoff:
    await store.set(session_id, session)

    # Load on new AudioSocket connection:
    session = await store.get(session_id)
    if session is None:
        session = CallSession()   # new call
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Optional

from core.session import CallSession

logger = logging.getLogger(__name__)


class SessionStore(ABC):
    """Abstract base — swap implementations without changing callers."""

    @abstractmethod
    async def get(self, session_id: str) -> Optional[CallSession]:
        """Return the session or None if not found / expired."""

    @abstractmethod
    async def set(self, session_id: str, session: CallSession) -> None:
        """Persist the session. Overwrites any existing entry."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove a session (called on clean call end)."""

    @abstractmethod
    async def close(self) -> None:
        """Release connections / resources."""


# ── In-memory fallback ────────────────────────────────────────────────────────

class InMemorySessionStore(SessionStore):
    """
    Single-process in-memory store.
    Used automatically when Redis is disabled or unavailable.
    NOT suitable for multi-pod deployments — sessions are not shared.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        logger.warning(
            "[SessionStore] Using IN-MEMORY store — sessions are NOT shared across pods. "
            "Set REDIS_ENABLED=true for production multi-worker deployments."
        )

    async def get(self, session_id: str) -> Optional[CallSession]:
        data = self._store.get(session_id)
        if data is None:
            return None
        try:
            return CallSession.from_dict(data)
        except Exception as e:
            logger.error(f"[SessionStore] Failed to deserialize session {session_id}: {e}")
            return None

    async def set(self, session_id: str, session: CallSession) -> None:
        self._store[session_id] = session.to_dict()

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    async def close(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ── Redis-backed store ────────────────────────────────────────────────────────

class RedisSessionStore(SessionStore):
    """
    Redis-backed session store for horizontal scaling.

    Each session is stored as a JSON string under key "session:{session_id}".
    TTL is set to `ttl_seconds` on every write — refreshed each turn.

    Thread-safe: redis asyncio client handles connection pooling internally.
    """

    _KEY_PREFIX = "session:"

    def __init__(self, client, ttl_seconds: int = 600) -> None:
        self._client = client
        self._ttl    = ttl_seconds
        logger.info(f"[SessionStore] Redis store initialized (ttl={ttl_seconds}s)")

    @classmethod
    async def create(cls, redis_url: str, ttl_seconds: int = 600) -> "RedisSessionStore":
        """
        Create and connect a RedisSessionStore.
        Raises ConnectionError if Redis is unreachable.
        """
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package not installed. Run: pip install redis[asyncio]"
            )
        client = await aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        # Verify connectivity
        await client.ping()
        logger.info(f"[SessionStore] Connected to Redis: {redis_url}")
        return cls(client, ttl_seconds)

    def _key(self, session_id: str) -> str:
        return f"{self._KEY_PREFIX}{session_id}"

    async def get(self, session_id: str) -> Optional[CallSession]:
        try:
            raw = await self._client.get(self._key(session_id))
            if raw is None:
                return None
            data = json.loads(raw)
            return CallSession.from_dict(data)
        except Exception as e:
            logger.error(f"[SessionStore] Redis GET error for {session_id}: {e}")
            return None

    async def set(self, session_id: str, session: CallSession) -> None:
        try:
            data = session.to_dict()
            raw  = json.dumps(data, ensure_ascii=False)
            await self._client.setex(self._key(session_id), self._ttl, raw)
        except Exception as e:
            logger.error(f"[SessionStore] Redis SET error for {session_id}: {e}")

    async def delete(self, session_id: str) -> None:
        try:
            await self._client.delete(self._key(session_id))
        except Exception as e:
            logger.error(f"[SessionStore] Redis DELETE error for {session_id}: {e}")

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass

    async def active_session_count(self) -> int:
        """Returns approximate count of active sessions (for monitoring)."""
        try:
            keys = await self._client.keys(f"{self._KEY_PREFIX}*")
            return len(keys)
        except Exception:
            return -1


# ── Factory ───────────────────────────────────────────────────────────────────

async def create_session_store(
    redis_enabled: bool,
    redis_url: str,
    ttl_seconds: int = 600,
) -> SessionStore:
    """
    Build the appropriate SessionStore based on configuration.
    Falls back to InMemorySessionStore if Redis is disabled or unreachable.
    """
    if not redis_enabled:
        logger.info("[SessionStore] Redis disabled — using in-memory store")
        return InMemorySessionStore()

    try:
        return await RedisSessionStore.create(redis_url, ttl_seconds)
    except ImportError as e:
        logger.error(f"[SessionStore] Redis import error: {e} — falling back to in-memory")
        return InMemorySessionStore()
    except Exception as e:
        logger.error(
            f"[SessionStore] Redis connection failed ({e}) — "
            "falling back to in-memory store. "
            "This is single-pod mode — not suitable for multi-worker deployments."
        )
        return InMemorySessionStore()
