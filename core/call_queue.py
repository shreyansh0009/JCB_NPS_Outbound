"""
CallGate — bounded concurrency control for incoming AudioSocket calls.

Problem:
  asyncio.start_server() accepts ALL incoming TCP connections immediately.
  Without a gate, a traffic spike can create thousands of active call coroutines,
  exhausting memory, Groq API rate limits, Deepgram connections, and ElevenLabs
  concurrent stream limits.

Solution:
  A semaphore gate with configurable max_concurrent_calls.

  Behaviour tiers:
  ┌──────────────────────┬────────────────────────────────────────────────────┐
  │ Active calls         │ Behaviour                                          │
  ├──────────────────────┼────────────────────────────────────────────────────┤
  │ < max_concurrent     │ Acquire immediately, proceed normally              │
  │ == max_concurrent    │ Wait up to queue_timeout_s for a slot              │
  │ Wait timed out       │ Send HANGUP frame, close TCP connection            │
  │ Queue itself full    │ Reject immediately (no slot in wait queue)         │
  └──────────────────────┴────────────────────────────────────────────────────┘

  When a call is rejected, Asterisk will hear the TCP connection close and
  can play hold music or retry. Configure Asterisk's audiohook retry setting
  to queue on its side for a better caller experience.

Sizing guide:
  - Each active call uses ~30-50MB RAM (Python asyncio + Deepgram WS + TTS buffer)
  - On a 4GB worker: max ~80 calls per worker safely
  - On a 16GB worker: max ~300 calls per worker
  - For 5K concurrent: 17+ workers × 300 = 5,100 capacity
  - HAProxy source-IP stickiness keeps Asterisk routing to the same worker

Usage (in api/audiosocket.py):
    gate = CallGate(max_concurrent=300, queue_timeout_s=10.0)

    async def handle_connection(reader, writer):
        async with gate.acquire(call_sid):
            await pipeline.run_audiosocket_call(call_sid, reader, writer)
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CallGateStats:
    """Real-time stats for health-check endpoints."""
    active:      int = 0
    queued:      int = 0
    total_calls: int = 0
    rejected:    int = 0
    peak_active: int = 0
    peak_queued: int = 0
    _start_time: float = field(default_factory=time.monotonic)

    def calls_per_minute(self) -> float:
        elapsed_minutes = (time.monotonic() - self._start_time) / 60.0
        if elapsed_minutes < 0.1:
            return 0.0
        return self.total_calls / elapsed_minutes


class CallGate:
    """
    Bounded semaphore gate for concurrent calls.

    Thread-safe via asyncio lock. All operations are non-blocking except
    for the `acquire()` wait when at capacity.
    """

    def __init__(
        self,
        max_concurrent: int = 500,
        queue_timeout_s: float = 15.0,
        max_queue_size:  int = 100,
    ):
        self._max_concurrent  = max_concurrent
        self._queue_timeout   = queue_timeout_s
        self._max_queue_size  = max_queue_size
        self._sem             = asyncio.Semaphore(max_concurrent)
        self._stats           = CallGateStats()
        self._lock            = asyncio.Lock()

        logger.info(
            f"[CallGate] Initialized: max_concurrent={max_concurrent} "
            f"queue_timeout={queue_timeout_s}s max_queue={max_queue_size}"
        )

    @asynccontextmanager
    async def acquire(self, call_sid: str):
        """
        Context manager that acquires a slot for one call.

        Usage:
            async with gate.acquire(call_sid) as accepted:
                if not accepted:
                    # Call was rejected — send HANGUP to Asterisk and return
                    return
                await pipeline.run_audiosocket_call(...)
        """
        accepted = await self._try_acquire(call_sid)
        try:
            yield accepted
        finally:
            if accepted:
                await self._release(call_sid)

    async def _try_acquire(self, call_sid: str) -> bool:
        """
        Try to acquire a slot. Returns True if acquired, False if rejected.
        Waits up to queue_timeout_s when at capacity.
        """
        async with self._lock:
            self._stats.total_calls += 1

            # Reject immediately if wait queue is also full
            if self._stats.queued >= self._max_queue_size:
                self._stats.rejected += 1
                logger.warning(
                    f"[CallGate] REJECTED immediately (queue full) — "
                    f"call_sid={call_sid} "
                    f"active={self._stats.active}/{self._max_concurrent} "
                    f"queued={self._stats.queued}/{self._max_queue_size}"
                )
                return False

            # Check if at capacity → enter queue
            at_capacity = self._stats.active >= self._max_concurrent
            if at_capacity:
                self._stats.queued += 1
                if self._stats.queued > self._stats.peak_queued:
                    self._stats.peak_queued = self._stats.queued

        if at_capacity:
            logger.info(
                f"[CallGate] QUEUED — call_sid={call_sid} "
                f"active={self._stats.active}/{self._max_concurrent} "
                f"queued={self._stats.queued} timeout={self._queue_timeout}s"
            )

        # Try to acquire with timeout
        try:
            acquired = await asyncio.wait_for(
                self._sem.acquire(),
                timeout=self._queue_timeout,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._stats.queued  = max(0, self._stats.queued - 1)
                self._stats.rejected += 1
            logger.warning(
                f"[CallGate] TIMEOUT after {self._queue_timeout}s — "
                f"call_sid={call_sid} — rejecting call"
            )
            return False

        async with self._lock:
            if at_capacity:
                self._stats.queued = max(0, self._stats.queued - 1)
            self._stats.active += 1
            if self._stats.active > self._stats.peak_active:
                self._stats.peak_active = self._stats.active

        logger.info(
            f"[CallGate] ACCEPTED — call_sid={call_sid} "
            f"active={self._stats.active}/{self._max_concurrent}"
        )
        return True

    async def _release(self, call_sid: str) -> None:
        """Release a slot after call ends."""
        self._sem.release()
        async with self._lock:
            self._stats.active = max(0, self._stats.active - 1)
        logger.debug(
            f"[CallGate] RELEASED — call_sid={call_sid} "
            f"active={self._stats.active}/{self._max_concurrent}"
        )

    def get_status(self) -> dict:
        """Returns a dict suitable for /health/capacity endpoint."""
        stats = self._stats
        return {
            "max_concurrent":    self._max_concurrent,
            "active_calls":      stats.active,
            "queued_calls":      stats.queued,
            "capacity_used_pct": round(stats.active / max(1, self._max_concurrent) * 100, 1),
            "total_calls":       stats.total_calls,
            "rejected_calls":    stats.rejected,
            "peak_active":       stats.peak_active,
            "peak_queued":       stats.peak_queued,
            "calls_per_minute":  round(stats.calls_per_minute(), 1),
            "queue_timeout_s":   self._queue_timeout,
            "max_queue_size":    self._max_queue_size,
        }

    @property
    def active_count(self) -> int:
        return self._stats.active

    @property
    def is_at_capacity(self) -> bool:
        return self._stats.active >= self._max_concurrent


# ── Distributed (Redis-backed) gate for cross-pod fairness ──────────────────


class DistributedCallGate:
    """
    Cross-pod concurrency limiter — wraps a local CallGate and adds a global
    Redis-backed counter so 4 pods × local_max=50 doesn't allow 200 concurrent
    when the global cap is 100.

    Algorithm (acquire):
      1. Acquire local CallGate slot (already enforces per-pod cap).
      2. Atomically INCR the Redis counter; if it exceeds `global_max`, DECR
         back, release the local slot, return False.
      3. Set/refresh TTL on the counter key so a crashed pod's slots don't
         leak forever (TTL = max_call_duration + grace).

    Release: DECR the Redis counter, release the local slot.

    Failure modes:
      - Redis unavailable -> falls back to local-only gate (logs once).
      - Slot leak from worker crash mid-call: the counter eventually expires
        when no acquire/release activity refreshes it.
    """

    _COUNTER_KEY = "callgate:active"

    def __init__(
        self,
        local: "CallGate",
        global_max: int,
        redis_client,
        slot_ttl_s: int = 700,
    ):
        self._local = local
        self._global_max = max(1, int(global_max))
        self._redis = redis_client
        self._ttl = slot_ttl_s
        self._redis_failed_once = False
        logger.info(
            f"[DistributedCallGate] global_max={global_max} "
            f"local_max={local._max_concurrent} ttl={slot_ttl_s}s"
        )

    @asynccontextmanager
    async def acquire(self, call_sid: str):
        # 1) local slot first (fast reject if pod is full)
        async with self._local.acquire(call_sid) as local_accepted:
            if not local_accepted:
                yield False
                return

            # 2) global counter
            global_accepted = await self._global_acquire(call_sid)
            try:
                yield global_accepted
            finally:
                if global_accepted:
                    await self._global_release(call_sid)

    async def _global_acquire(self, call_sid: str) -> bool:
        if self._redis is None:
            return True
        try:
            new_active = await self._redis.incr(self._COUNTER_KEY)
            await self._redis.expire(self._COUNTER_KEY, self._ttl)
            if new_active > self._global_max:
                # Roll back our INCR — we did not get a slot
                await self._redis.decr(self._COUNTER_KEY)
                logger.warning(
                    f"[DistributedCallGate] REJECTED globally (active={new_active}>{self._global_max}) "
                    f"call_sid={call_sid}"
                )
                return False
            return True
        except Exception as e:
            if not self._redis_failed_once:
                logger.error(
                    f"[DistributedCallGate] Redis unavailable ({e}); "
                    "falling back to local-only gate"
                )
                self._redis_failed_once = True
            return True

    async def _global_release(self, call_sid: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.decr(self._COUNTER_KEY)
        except Exception as e:
            logger.debug(f"[DistributedCallGate] release error for {call_sid}: {e}")

    async def get_global_active(self) -> int:
        if self._redis is None:
            return -1
        try:
            v = await self._redis.get(self._COUNTER_KEY)
            return int(v) if v is not None else 0
        except Exception:
            return -1

    def get_status(self) -> dict:
        out = self._local.get_status()
        out["distributed"] = {
            "global_max": self._global_max,
            "ttl_s": self._ttl,
        }
        return out

    @property
    def active_count(self) -> int:
        return self._local.active_count
