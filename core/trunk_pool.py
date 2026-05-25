"""
SIP trunk pool with failover + caller-ID rotation.

Why this exists:
  Production telephony needs (a) more than one upstream trunk so a single
  carrier outage doesn't take the whole system down, and (b) more than one
  outbound caller-ID to comply with carrier reputation/spam-flagging rules
  and TCPA-style per-campaign rotation.

Configuration:
  TRUNKS env var as JSON list of objects, each with:
    {"name": "webphoneuri", "caller_ids": ["+917935459108", "+917935459107"],
     "prefix": "922", "weight": 1}

  If TRUNKS is unset, the pool falls back to legacy single-trunk mode using
  SIP_TRUNK / OUTBOUND_CALLER_ID / SIP_PREFIX (Phase 1/2 behaviour preserved).

Failure model:
  Each trunk has a circuit-breaker-style health window. A trunk that fails
  N times within COOLDOWN_S is marked unhealthy and skipped on subsequent
  acquire() calls until cooldown expires. Caller-IDs within a trunk rotate
  round-robin so no single number sees a sudden volume spike.

Usage:
    pool = TrunkPool.from_settings(settings)
    binding = pool.acquire()                        # → (trunk, caller_id, prefix)
    try:
        await ami.originate(channel=binding.channel(mobile), caller_id=binding.caller_id, ...)
        pool.report_success(binding)
    except Exception as e:
        pool.report_failure(binding, e)
        raise
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_FAILURE_THRESHOLD = 3   # consecutive failures to mark unhealthy
_DEFAULT_COOLDOWN_S = 60.0       # seconds before a failed trunk is retried


@dataclass
class _TrunkConfig:
    name: str
    caller_ids: list[str]
    prefix: str = ""
    weight: int = 1
    # Runtime state
    failure_count: int = 0
    cooldown_until: float = 0.0
    cid_index: int = 0
    last_used_at: float = 0.0


@dataclass
class TrunkBinding:
    """A single dial slot — one trunk + one caller-ID + one prefix."""
    trunk_name: str
    caller_id: str
    prefix: str

    def channel(self, mobile: str) -> str:
        """Build the PJSIP channel string the way Asterisk expects it."""
        return f"PJSIP/{self.prefix}{self.caller_id}{mobile}@{self.trunk_name}"


class TrunkPool:
    """Round-robin trunk selection with health-checked failover + CID rotation."""

    def __init__(
        self,
        trunks: list[_TrunkConfig],
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_s: float = _DEFAULT_COOLDOWN_S,
    ):
        if not trunks:
            raise ValueError("TrunkPool requires at least one trunk")
        self._trunks = trunks
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._rr_index = 0
        logger.info(
            f"[TrunkPool] {len(trunks)} trunk(s), "
            f"failure_threshold={failure_threshold} cooldown={cooldown_s}s"
        )

    # ── Acquisition ────────────────────────────────────────────────────────

    def acquire(self) -> TrunkBinding:
        """
        Return the next healthy trunk + a rotating caller-ID. If all trunks
        are unhealthy, the LEAST recently failed one is returned anyway —
        propagating the dial attempt is better than refusing all calls.
        """
        now = time.monotonic()
        n = len(self._trunks)

        # First pass: round-robin healthy trunks (weighted by `weight`).
        for _ in range(n):
            idx = self._rr_index % n
            self._rr_index += 1
            t = self._trunks[idx]
            if t.cooldown_until <= now:
                return self._bind(t, now)

        # All in cooldown — fall through to the one nearest to recovery.
        t = min(self._trunks, key=lambda x: x.cooldown_until)
        logger.warning(
            f"[TrunkPool] all trunks unhealthy; using least-stale '{t.name}' "
            f"(cooldown_remaining={max(0, t.cooldown_until - now):.1f}s)"
        )
        return self._bind(t, now)

    def _bind(self, t: _TrunkConfig, now: float) -> TrunkBinding:
        cid = t.caller_ids[t.cid_index % len(t.caller_ids)]
        t.cid_index += 1
        t.last_used_at = now
        return TrunkBinding(trunk_name=t.name, caller_id=cid, prefix=t.prefix)

    # ── Health reporting ──────────────────────────────────────────────────

    def report_success(self, binding: TrunkBinding) -> None:
        t = self._lookup(binding.trunk_name)
        if t is None:
            return
        if t.failure_count > 0:
            logger.info(f"[TrunkPool] '{t.name}' recovered (was failure_count={t.failure_count})")
        t.failure_count = 0
        t.cooldown_until = 0.0

    def report_failure(self, binding: TrunkBinding, error: object | None = None) -> None:
        t = self._lookup(binding.trunk_name)
        if t is None:
            return
        t.failure_count += 1
        if t.failure_count >= self._failure_threshold:
            t.cooldown_until = time.monotonic() + self._cooldown_s
            logger.warning(
                f"[TrunkPool] '{t.name}' marked unhealthy after {t.failure_count} failures "
                f"(cooldown {self._cooldown_s}s) error={error!r}"
            )

    def _lookup(self, name: str) -> Optional[_TrunkConfig]:
        for t in self._trunks:
            if t.name == name:
                return t
        return None

    # ── Status / introspection ────────────────────────────────────────────

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "trunks": [
                {
                    "name": t.name,
                    "healthy": t.cooldown_until <= now,
                    "failure_count": t.failure_count,
                    "cooldown_remaining_s": max(0.0, t.cooldown_until - now),
                    "caller_ids": t.caller_ids,
                    "next_caller_id_index": t.cid_index % len(t.caller_ids),
                }
                for t in self._trunks
            ],
        }

    @property
    def size(self) -> int:
        return len(self._trunks)

    # ── Factory ───────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings) -> "TrunkPool":
        """
        Build a TrunkPool from settings. If `TRUNKS` env JSON is present, use it.
        Otherwise, fall back to legacy single-trunk config (SIP_TRUNK / etc).
        """
        raw = os.getenv("TRUNKS", "").strip()
        if raw:
            try:
                data = json.loads(raw)
                trunks: list[_TrunkConfig] = []
                for entry in data:
                    name = (entry.get("name") or "").strip()
                    if not name:
                        continue
                    cids = entry.get("caller_ids") or []
                    if isinstance(cids, str):
                        cids = [cids]
                    cids = [c for c in (str(x).strip() for x in cids) if c]
                    if not cids:
                        # If no caller_ids supplied, fall back to settings default
                        if settings.outbound_caller_id:
                            cids = [settings.outbound_caller_id]
                        else:
                            logger.warning(f"[TrunkPool] trunk '{name}' has no caller_ids — skipping")
                            continue
                    trunks.append(_TrunkConfig(
                        name=name,
                        caller_ids=cids,
                        prefix=str(entry.get("prefix", settings.sip_prefix or "")),
                        weight=int(entry.get("weight", 1)),
                    ))
                if trunks:
                    return cls(
                        trunks,
                        failure_threshold=int(os.getenv("TRUNK_FAILURE_THRESHOLD", _DEFAULT_FAILURE_THRESHOLD)),
                        cooldown_s=float(os.getenv("TRUNK_COOLDOWN_S", _DEFAULT_COOLDOWN_S)),
                    )
                logger.warning("[TrunkPool] TRUNKS env present but parsed to zero trunks; falling back to legacy")
            except Exception as e:
                logger.error(f"[TrunkPool] failed to parse TRUNKS env ({e}); falling back to legacy single-trunk")

        # Legacy single-trunk fallback (preserves Phase 1/2 behaviour)
        if not settings.sip_trunk:
            raise RuntimeError("No SIP trunk configured (set SIP_TRUNK or TRUNKS env)")
        return cls(
            [_TrunkConfig(
                name=settings.sip_trunk,
                caller_ids=[settings.outbound_caller_id] if settings.outbound_caller_id else [settings.sip_trunk],
                prefix=settings.sip_prefix or "",
            )]
        )


# ── Module-level singleton ───────────────────────────────────────────────────

_pool: Optional[TrunkPool] = None


def configure(pool: TrunkPool) -> None:
    global _pool
    _pool = pool


def get_pool() -> Optional[TrunkPool]:
    return _pool
