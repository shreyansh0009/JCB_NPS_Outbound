"""
Token-bucket rate limiter, scoped per tenant.

Why this exists:
  Without this, one noisy tenant can saturate Groq / Cartesia / Deepgram
  rate limits and starve every other tenant on the same worker.

Algorithm:
  Standard token bucket — `capacity` tokens, refills at `rate` tokens/sec.
  acquire(n=1) returns True immediately if n tokens are available, else False
  (non-blocking). awaitable_acquire() awaits up to `wait_s` for tokens.

Configuration sources (in order of precedence):
  1. config/tenants.json — per-DID `rate_limit: {capacity, rate_per_sec}` block
  2. RATE_LIMIT_CAPACITY / RATE_LIMIT_RATE_PER_SEC env (global default)
  3. Hardcoded safe defaults (capacity=10, rate=2/s = ~120 calls/min/tenant)

Usage:
    bucket = TenantRateLimiter.get("montra_primary")
    if not bucket.try_acquire():
        raise RateLimited(...)

    # or
    ok = await TenantRateLimiter.get("montra_primary").wait_acquire(timeout=2.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_DEFAULT_CAPACITY = 10
_DEFAULT_RATE_PER_SEC = 2.0   # ≈ 120/min steady state, capacity allows bursts up to 10


@dataclass
class _Bucket:
    capacity: float
    rate_per_sec: float
    tokens: float
    last_refill: float

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            self.last_refill = now

    def try_take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def time_until(self, n: float = 1.0) -> float:
        """Seconds until `n` tokens will be available, given current refill rate."""
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= n:
            return 0.0
        if self.rate_per_sec <= 0:
            return float("inf")
        return (n - self.tokens) / self.rate_per_sec


class TenantRateLimiter:
    """
    Per-tenant token-bucket pool. Lazy-creates a bucket on first reference.
    Process-local — for cross-pod fairness, swap the dict for a Redis impl.
    """

    _buckets: dict[str, _Bucket] = {}
    _per_tenant_overrides: dict[str, tuple[float, float]] = {}

    @classmethod
    def configure(cls, defaults: tuple[float, float] | None = None,
                  per_tenant: dict[str, tuple[float, float]] | None = None) -> None:
        """
        defaults:    (capacity, rate_per_sec)
        per_tenant:  {tenant_id: (capacity, rate_per_sec)}
        """
        if defaults is not None:
            cls._defaults = defaults  # type: ignore[attr-defined]
        if per_tenant:
            cls._per_tenant_overrides.update(per_tenant)
            logger.info(f"[RateLimiter] per-tenant overrides loaded: {list(per_tenant.keys())}")

    @classmethod
    def _defaults_pair(cls) -> tuple[float, float]:
        cap = float(os.getenv("RATE_LIMIT_CAPACITY", _DEFAULT_CAPACITY))
        rate = float(os.getenv("RATE_LIMIT_RATE_PER_SEC", _DEFAULT_RATE_PER_SEC))
        return getattr(cls, "_defaults", (cap, rate))

    @classmethod
    def get(cls, tenant_id: str) -> _Bucket:
        b = cls._buckets.get(tenant_id)
        if b is None:
            cap, rate = cls._per_tenant_overrides.get(tenant_id, cls._defaults_pair())
            b = _Bucket(
                capacity=float(cap),
                rate_per_sec=float(rate),
                tokens=float(cap),
                last_refill=time.monotonic(),
            )
            cls._buckets[tenant_id] = b
            logger.info(f"[RateLimiter] bucket created for tenant '{tenant_id}': cap={cap} rate={rate}/s")
        return b

    @classmethod
    def try_acquire(cls, tenant_id: str, n: float = 1.0) -> bool:
        return cls.get(tenant_id).try_take(n)

    @classmethod
    async def wait_acquire(cls, tenant_id: str, n: float = 1.0, timeout: float = 5.0) -> bool:
        """
        Wait up to `timeout` seconds for `n` tokens. Returns True if acquired,
        False on timeout. Polls at most every 100ms — sufficient resolution
        for telephony rate-limiting.
        """
        deadline = time.monotonic() + timeout
        while True:
            if cls.try_acquire(tenant_id, n):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait_s = min(0.1, max(0.0, remaining))
            await asyncio.sleep(wait_s)

    @classmethod
    def status(cls) -> dict:
        out = {}
        now = time.monotonic()
        for tid, b in cls._buckets.items():
            b._refill(now)
            out[tid] = {
                "tokens": round(b.tokens, 2),
                "capacity": b.capacity,
                "rate_per_sec": b.rate_per_sec,
            }
        return out


def load_tenant_overrides_from_json(path: str) -> dict[str, tuple[float, float]]:
    """
    Read per-tenant rate limits from config/tenants.json. Each tenant entry
    may include a "rate_limit": {"capacity": N, "rate_per_sec": N} block.
    """
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        logger.warning(f"[RateLimiter] cannot read {path}: {e}")
        return {}

    out: dict[str, tuple[float, float]] = {}
    # default block
    d = data.get("default", {})
    if "rate_limit" in d:
        rl = d["rate_limit"]
        out[d.get("tenant_id", "default")] = (
            float(rl.get("capacity", _DEFAULT_CAPACITY)),
            float(rl.get("rate_per_sec", _DEFAULT_RATE_PER_SEC)),
        )
    # per-DID blocks
    for _did, cfg in (data.get("dids", {}) or {}).items():
        if "rate_limit" in cfg:
            rl = cfg["rate_limit"]
            out[cfg.get("tenant_id", _did)] = (
                float(rl.get("capacity", _DEFAULT_CAPACITY)),
                float(rl.get("rate_per_sec", _DEFAULT_RATE_PER_SEC)),
            )
    return out
