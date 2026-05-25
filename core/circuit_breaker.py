"""
CircuitBreaker — provider fault isolation for enterprise voice agent.

Implements the standard three-state circuit breaker pattern:

  CLOSED    → Normal operation. Failures are counted.
  OPEN      → Provider is failing. Requests rejected immediately (fast-fail).
              No load sent to the failing provider during recovery_timeout.
  HALF_OPEN → After recovery_timeout, one probe request is allowed through.
              Success → CLOSED. Failure → back to OPEN.

Why this matters at scale:
  At 5K concurrent calls, a Groq timeout without a circuit breaker means
  5K calls all wait 4 seconds before failing. With a breaker, after 5
  failures the breaker opens and subsequent calls fail in <1ms, allowing
  the fallback provider to take over instantly.

Usage:
    breaker = CircuitBreaker("groq", failure_threshold=5, recovery_timeout=30.0)

    async def call_llm():
        async with breaker.protect():
            return await groq.chat(messages)

    # Or manual:
    if breaker.allow_request():
        try:
            result = await groq.chat(messages)
            breaker.record_success()
            return result
        except Exception as e:
            breaker.record_failure(e)
            raise
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Type

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a request is rejected because the circuit is open."""
    def __init__(self, name: str, retry_in: float):
        self.name = name
        self.retry_in = retry_in
        super().__init__(
            f"Circuit '{name}' is OPEN — provider unavailable. "
            f"Retry in {retry_in:.1f}s"
        )


@dataclass
class CircuitStats:
    """Rolling stats for monitoring / Prometheus export."""
    total_calls:    int = 0
    total_failures: int = 0
    total_rejected: int = 0   # calls rejected by open circuit
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    last_state_change: float = field(default_factory=time.monotonic)
    state_history: list[tuple[float, str]] = field(default_factory=list)


class CircuitBreaker:
    """
    Thread-safe (asyncio-safe) circuit breaker for external provider calls.

    Parameters
    ----------
    name              : Human-readable provider name (for logs/metrics)
    failure_threshold : Consecutive failures before opening the circuit
    recovery_timeout  : Seconds to wait in OPEN state before probing
    half_open_max     : Consecutive successes in HALF_OPEN needed to close
    excluded_exceptions: Exception types that do NOT count as failures
                         (e.g., validation errors, not provider errors)
    on_state_change   : Optional callback(name, old_state, new_state)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 2,
        excluded_exceptions: tuple[Type[Exception], ...] = (),
        on_state_change: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.name               = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout  = recovery_timeout
        self._half_open_max     = half_open_max
        self._excluded          = excluded_exceptions
        self._on_state_change   = on_state_change

        self._state             = CircuitState.CLOSED
        self._half_open_ok      = 0        # successes in HALF_OPEN
        self._lock              = asyncio.Lock()
        self.stats              = CircuitStats()

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        if self._state != CircuitState.OPEN:
            return False
        # Check if recovery_timeout has elapsed → transition to HALF_OPEN
        elapsed = time.monotonic() - self.stats.last_failure_time
        if elapsed >= self._recovery_timeout:
            # Don't acquire lock here — allow_request will handle it
            return False
        return True

    def allow_request(self) -> bool:
        """
        Returns True if the request should proceed.
        Side-effect: may transition OPEN → HALF_OPEN.
        """
        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.HALF_OPEN:
            # Only allow one probe at a time
            return self._half_open_ok < self._half_open_max

        # OPEN — check if recovery window has passed
        elapsed = time.monotonic() - self.stats.last_failure_time
        if elapsed >= self._recovery_timeout:
            self._transition(CircuitState.HALF_OPEN)
            return True

        self.stats.total_rejected += 1
        retry_in = self._recovery_timeout - elapsed
        logger.debug(f"[CircuitBreaker:{self.name}] OPEN — rejecting request. Retry in {retry_in:.1f}s")
        return False

    def record_success(self) -> None:
        """Call after a successful provider response."""
        self.stats.total_calls    += 1
        self.stats.last_success_time = time.monotonic()
        self.stats.consecutive_failures = 0

        if self._state == CircuitState.HALF_OPEN:
            self._half_open_ok += 1
            if self._half_open_ok >= self._half_open_max:
                self._transition(CircuitState.CLOSED)

    def record_failure(self, exc: Optional[Exception] = None) -> None:
        """Call after a provider error."""
        if exc is not None and isinstance(exc, self._excluded):
            logger.debug(f"[CircuitBreaker:{self.name}] Excluded exception — not counted: {type(exc).__name__}")
            return

        self.stats.total_calls    += 1
        self.stats.total_failures += 1
        self.stats.consecutive_failures += 1
        self.stats.last_failure_time = time.monotonic()

        logger.warning(
            f"[CircuitBreaker:{self.name}] Failure #{self.stats.consecutive_failures} "
            f"(threshold={self._failure_threshold}) — {type(exc).__name__ if exc else 'unknown'}"
        )

        if self._state == CircuitState.HALF_OPEN:
            # Any failure in HALF_OPEN → back to OPEN
            self._half_open_ok = 0
            self._transition(CircuitState.OPEN)

        elif self._state == CircuitState.CLOSED:
            if self.stats.consecutive_failures >= self._failure_threshold:
                self._transition(CircuitState.OPEN)

    @asynccontextmanager
    async def protect(self):
        """
        Async context manager. Use this to wrap provider calls:

            async with breaker.protect():
                result = await provider.call(...)
        """
        async with self._lock:
            if not self.allow_request():
                elapsed = time.monotonic() - self.stats.last_failure_time
                retry_in = max(0.0, self._recovery_timeout - elapsed)
                raise CircuitOpenError(self.name, retry_in)

        try:
            yield
            async with self._lock:
                self.record_success()
        except CircuitOpenError:
            raise
        except Exception as e:
            async with self._lock:
                self.record_failure(e)
            raise

    def reset(self) -> None:
        """Force-close the circuit (e.g., after manual intervention)."""
        self._transition(CircuitState.CLOSED)
        self.stats.consecutive_failures = 0
        self._half_open_ok = 0
        logger.info(f"[CircuitBreaker:{self.name}] Manually reset → CLOSED")

    def get_status(self) -> dict:
        """Returns a dict suitable for health-check endpoints."""
        elapsed_since_failure = (
            time.monotonic() - self.stats.last_failure_time
            if self.stats.last_failure_time > 0 else None
        )
        retry_in = None
        if self._state == CircuitState.OPEN and elapsed_since_failure is not None:
            retry_in = max(0.0, self._recovery_timeout - elapsed_since_failure)

        return {
            "name":                  self.name,
            "state":                 self._state.value,
            "consecutive_failures":  self.stats.consecutive_failures,
            "total_calls":           self.stats.total_calls,
            "total_failures":        self.stats.total_failures,
            "total_rejected":        self.stats.total_rejected,
            "failure_threshold":     self._failure_threshold,
            "recovery_timeout_s":    self._recovery_timeout,
            "retry_in_s":            round(retry_in, 1) if retry_in else None,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _transition(self, new_state: CircuitState) -> None:
        old_state = self._state
        if old_state == new_state:
            return

        self._state = new_state
        self._half_open_ok = 0
        now = time.monotonic()
        self.stats.last_state_change = now
        self.stats.state_history.append((now, new_state.value))

        # Keep only last 20 transitions
        if len(self.stats.state_history) > 20:
            self.stats.state_history = self.stats.state_history[-20:]

        level = logging.WARNING if new_state == CircuitState.OPEN else logging.INFO
        logger.log(
            level,
            f"[CircuitBreaker:{self.name}] {old_state.value.upper()} → {new_state.value.upper()}"
            + (f" (consecutive_failures={self.stats.consecutive_failures})"
               if new_state == CircuitState.OPEN else "")
        )

        # Sprint 4: Prometheus metrics
        try:
            from core.metrics import CIRCUIT_BREAKER_STATE, CIRCUIT_BREAKER_TRIPS, CIRCUIT_BREAKER_RECOVERIES, CB_STATE_VALUES
            CIRCUIT_BREAKER_STATE.labels(provider=self.name).set(
                CB_STATE_VALUES.get(new_state.value, 0)
            )
            if new_state == CircuitState.OPEN:
                CIRCUIT_BREAKER_TRIPS.labels(provider=self.name).inc()
            elif new_state == CircuitState.CLOSED and old_state != CircuitState.CLOSED:
                CIRCUIT_BREAKER_RECOVERIES.labels(provider=self.name).inc()
        except Exception:
            pass  # never let metrics break the circuit breaker itself

        if self._on_state_change:
            try:
                self._on_state_change(self.name, old_state.value, new_state.value)
            except Exception:
                pass


# ── Registry — one breaker per provider, shared across all calls ──────────────

class CircuitBreakerRegistry:
    """
    Singleton registry. Breakers are shared across all concurrent calls
    so that failures from any call contribute to the shared failure count.

    This is the correct behaviour at scale: if Groq is down, the circuit
    should open based on failures from ALL 5K concurrent calls, not per-call.
    """
    _breakers: dict[str, CircuitBreaker] = {}

    @classmethod
    def get(cls, name: str) -> Optional[CircuitBreaker]:
        return cls._breakers.get(name)

    @classmethod
    def register(
        cls,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 2,
        excluded_exceptions: tuple = (),
    ) -> CircuitBreaker:
        if name not in cls._breakers:
            cls._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                half_open_max=half_open_max,
                excluded_exceptions=excluded_exceptions,
            )
            logger.info(
                f"[CircuitBreakerRegistry] Registered: {name} "
                f"(threshold={failure_threshold}, recovery={recovery_timeout}s)"
            )
        return cls._breakers[name]

    @classmethod
    def get_all_status(cls) -> dict[str, dict]:
        return {name: cb.get_status() for name, cb in cls._breakers.items()}

    @classmethod
    def reset_all(cls) -> None:
        for cb in cls._breakers.values():
            cb.reset()


# ── Pre-register all known providers at import time ───────────────────────────

# LLM providers
CircuitBreakerRegistry.register(
    "groq",
    failure_threshold=5,
    recovery_timeout=30.0,
    half_open_max=2,
)
CircuitBreakerRegistry.register(
    "groq_fallback",
    failure_threshold=3,
    recovery_timeout=20.0,
    half_open_max=1,
)
CircuitBreakerRegistry.register(
    "ollama",
    failure_threshold=3,
    recovery_timeout=15.0,
    half_open_max=2,
)

# STT providers
CircuitBreakerRegistry.register(
    "deepgram",
    failure_threshold=3,   # STT failures are more disruptive — open faster
    recovery_timeout=20.0,
    half_open_max=1,
)
CircuitBreakerRegistry.register(
    "whisper",
    failure_threshold=5,
    recovery_timeout=30.0,
    half_open_max=2,
)

# TTS providers
CircuitBreakerRegistry.register(
    "elevenlabs",
    failure_threshold=3,
    recovery_timeout=20.0,
    half_open_max=1,
)
CircuitBreakerRegistry.register(
    "cartesia",
    failure_threshold=3,
    recovery_timeout=20.0,
    half_open_max=1,
)
