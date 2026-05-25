"""
UUID -> outbound-lead context map.

When we Originate an outbound call, we know the customer's name/mobile/product/
issue up front but Asterisk hasn't opened the AudioSocket yet. We stash the
lead context keyed by the call UUID here; when the pipeline receives the
AudioSocket UUID frame, it pops the context and seeds the session.

Backends:
  - InMemoryBackend  : module-level dict (single-process; default)
  - RedisBackend     : HSET + ZSET indexed by _ts (multi-process / horizontal scale)

The recency-fallback ("UUID I got from Asterisk doesn't match the one we stored,
take the newest entry within window_secs") is preserved across both backends.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# Stash key prefix in Redis. ZSET tracks recency for the fallback path.
_KEY_PREFIX = "outbound_lead:"
_RECENT_ZSET = "outbound_lead:_recent"


class _Backend(Protocol):
    async def put(self, uuid_str: str, lead: dict) -> None: ...
    async def pop(self, uuid_str: str, window_secs: float = 10.0) -> dict: ...
    async def peek(self, uuid_str: str) -> Optional[dict]: ...
    async def peek_newest(self) -> Optional[dict]: ...
    async def size(self) -> int: ...
    async def snapshot(self) -> dict: ...


# ── In-memory backend (default) ──────────────────────────────────────────────

class InMemoryBackend:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def put(self, uuid_str: str, lead: dict) -> None:
        entry = dict(lead)
        entry["_ts"] = time.monotonic()
        self._store[uuid_str] = entry
        logger.info(f"[outbound_call_store/mem] put uuid={uuid_str} name={lead.get('name')}")

    async def pop(self, uuid_str: str, window_secs: float = 10.0) -> dict:
        entry = self._store.pop(uuid_str, None)
        if entry is not None:
            return entry
        if not self._store:
            return {}
        now = time.monotonic()
        newest_key, newest_entry = max(
            self._store.items(), key=lambda kv: kv[1].get("_ts", 0)
        )
        if now - newest_entry.get("_ts", 0) < window_secs:
            logger.info(
                f"[outbound_call_store/mem] UUID {uuid_str} mismatch; "
                f"matched newest ({newest_key}) within {window_secs}s window"
            )
            return self._store.pop(newest_key, {})
        return {}

    async def peek(self, uuid_str: str) -> Optional[dict]:
        return self._store.get(uuid_str)

    async def peek_newest(self) -> Optional[dict]:
        if not self._store:
            return None
        return max(self._store.values(), key=lambda e: e.get("_ts", 0))

    async def size(self) -> int:
        return len(self._store)

    async def snapshot(self) -> dict:
        return dict(self._store)


# ── Redis backend ────────────────────────────────────────────────────────────

class RedisBackend:
    """
    Lead context lives in two structures:
      KEY  outbound_lead:{uuid}   = JSON-encoded lead dict (with _ts as unix time)
      ZSET outbound_lead:_recent  = score: unix time, member: uuid

    Both use the same TTL (refreshed on put). The ZSET trims old entries lazily
    on peek_newest; ZSET members for already-popped/expired keys are filtered out.
    """

    def __init__(self, client, ttl_seconds: int = 120) -> None:
        self._client = client
        self._ttl = ttl_seconds

    @classmethod
    async def create(cls, redis_url: str, ttl_seconds: int = 120) -> "RedisBackend":
        try:
            import redis.asyncio as aioredis
        except ImportError as e:
            raise ImportError("redis package not installed: pip install redis") from e
        client = await aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        await client.ping()
        logger.info(f"[outbound_call_store/redis] Connected: {redis_url} (ttl={ttl_seconds}s)")
        return cls(client, ttl_seconds)

    @staticmethod
    def _key(uuid_str: str) -> str:
        return f"{_KEY_PREFIX}{uuid_str}"

    async def put(self, uuid_str: str, lead: dict) -> None:
        entry = dict(lead)
        entry["_ts"] = time.time()  # wall clock — Redis is across processes
        try:
            pipe = self._client.pipeline()
            pipe.setex(self._key(uuid_str), self._ttl, json.dumps(entry, ensure_ascii=False))
            pipe.zadd(_RECENT_ZSET, {uuid_str: entry["_ts"]})
            pipe.expire(_RECENT_ZSET, self._ttl)
            await pipe.execute()
            logger.info(f"[outbound_call_store/redis] put uuid={uuid_str} name={lead.get('name')}")
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] put failed for {uuid_str}: {e}")
            raise

    async def pop(self, uuid_str: str, window_secs: float = 10.0) -> dict:
        try:
            raw = await self._client.get(self._key(uuid_str))
            if raw is not None:
                # Atomic delete + cleanup ZSET entry
                pipe = self._client.pipeline()
                pipe.delete(self._key(uuid_str))
                pipe.zrem(_RECENT_ZSET, uuid_str)
                await pipe.execute()
                return json.loads(raw)
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] pop failed for {uuid_str}: {e}")
            return {}

        # Recency fallback: newest UUID within window_secs
        try:
            now = time.time()
            cutoff = now - window_secs
            # Latest entry (highest score)
            latest = await self._client.zrevrange(_RECENT_ZSET, 0, 0, withscores=True)
            if not latest:
                return {}
            newest_uuid, newest_ts = latest[0]
            if newest_ts < cutoff:
                return {}
            raw = await self._client.get(self._key(newest_uuid))
            if raw is None:
                # ZSET entry stale; clean it up
                await self._client.zrem(_RECENT_ZSET, newest_uuid)
                return {}
            pipe = self._client.pipeline()
            pipe.delete(self._key(newest_uuid))
            pipe.zrem(_RECENT_ZSET, newest_uuid)
            await pipe.execute()
            logger.info(
                f"[outbound_call_store/redis] UUID {uuid_str} mismatch; "
                f"matched newest ({newest_uuid}) within {window_secs}s window"
            )
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] recency fallback failed: {e}")
            return {}

    async def peek(self, uuid_str: str) -> Optional[dict]:
        try:
            raw = await self._client.get(self._key(uuid_str))
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] peek failed: {e}")
            return None

    async def peek_newest(self) -> Optional[dict]:
        try:
            latest = await self._client.zrevrange(_RECENT_ZSET, 0, 0)
            if not latest:
                return None
            raw = await self._client.get(self._key(latest[0]))
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] peek_newest failed: {e}")
            return None

    async def size(self) -> int:
        try:
            return int(await self._client.zcard(_RECENT_ZSET))
        except Exception:
            return -1

    async def snapshot(self) -> dict:
        # Used by dashboards. Bounded to last 100 entries.
        try:
            uuids = await self._client.zrevrange(_RECENT_ZSET, 0, 99)
            if not uuids:
                return {}
            keys = [self._key(u) for u in uuids]
            values = await self._client.mget(keys)
            out: dict = {}
            for u, raw in zip(uuids, values):
                if raw:
                    try:
                        out[u] = json.loads(raw)
                    except Exception:
                        continue
            return out
        except Exception as e:
            logger.error(f"[outbound_call_store/redis] snapshot failed: {e}")
            return {}

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


# ── Module-level singleton + facade ──────────────────────────────────────────

_backend: _Backend = InMemoryBackend()


def configure(backend: _Backend) -> None:
    """Swap the active backend (call once during startup)."""
    global _backend
    _backend = backend
    logger.info(f"[outbound_call_store] backend = {type(backend).__name__}")


def current_backend() -> _Backend:
    return _backend


async def put(uuid_str: str, lead: dict) -> None:
    await _backend.put(uuid_str, lead)


async def pop(uuid_str: str, window_secs: float = 10.0) -> dict:
    """Remove and return the lead context. Falls back to newest entry within window."""
    return await _backend.pop(uuid_str, window_secs)


async def peek(uuid_str: str) -> Optional[dict]:
    return await _backend.peek(uuid_str)


async def peek_newest() -> Optional[dict]:
    return await _backend.peek_newest()


async def size() -> int:
    return await _backend.size()


async def snapshot() -> dict:
    return await _backend.snapshot()


# ── Initialization helper ────────────────────────────────────────────────────

async def init_from_settings(redis_enabled: bool, redis_url: str, ttl_seconds: int = 120) -> None:
    """
    Configure the backend at startup. Falls back to InMemoryBackend on any
    Redis failure so the app stays up — operators can fix Redis without
    breaking telephony.
    """
    if not redis_enabled:
        logger.info("[outbound_call_store] Redis disabled — using in-memory backend")
        configure(InMemoryBackend())
        return
    try:
        backend = await RedisBackend.create(redis_url, ttl_seconds)
        configure(backend)
    except Exception as e:
        logger.error(
            f"[outbound_call_store] Redis init failed ({e}) — falling back to in-memory. "
            "This is single-pod mode; outbound state will be lost on restart."
        )
        configure(InMemoryBackend())
