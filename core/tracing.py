"""
Optional OpenTelemetry tracing shim.

Why a shim:
  We want trace points sprinkled across the hot path (AMI, AudioSocket, STT,
  LLM, TTS) without making OpenTelemetry a hard dependency. When OTEL is off
  (default) or the SDK isn't installed, `span(...)` is a no-op context manager
  and `set_attr` / `add_event` are cheap functions. When OTEL is enabled at
  startup, the shim resolves to the real OTel API.

Configure once in main.py:
    from core.tracing import init_tracing
    init_tracing(enabled=True, service_name="montra-outbound", endpoint="...")

Use anywhere:
    from core.tracing import span, set_attr
    async with span("llm.stream_chat", attrs={"model": self.model}):
        ...
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_tracer: Any = None  # set by init_tracing()
_enabled: bool = False


def init_tracing(
    enabled: bool = False,
    service_name: str = "montra-outbound",
    endpoint: str = "",
) -> bool:
    """
    Initialize OTel. Returns True iff tracing is now active.
    Safe to call multiple times — second call is a no-op.
    """
    global _tracer, _enabled
    if _enabled:
        return True
    if not enabled:
        logger.info("[tracing] OTel disabled (OTEL_ENABLED=false)")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "[tracing] opentelemetry SDK not installed — running without traces. "
            "Install: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
        )
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = None
    ep = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if ep:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=ep, insecure=True)
        except ImportError:
            logger.warning("[tracing] OTLP exporter not installed; falling back to console")
    if exporter is None:
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            exporter = ConsoleSpanExporter()
        except Exception:
            exporter = None

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    _enabled = True
    logger.info(f"[tracing] OTel active service={service_name} endpoint={ep or 'console'}")
    return True


@contextlib.contextmanager
def span(name: str, attrs: Optional[dict] = None) -> Iterator[Any]:
    """
    Sync span context manager. No-op when tracing is disabled.

    Yields the span (or None if disabled) so callers can attach attributes
    or events incrementally.
    """
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        if attrs:
            for k, v in attrs.items():
                try:
                    s.set_attribute(k, v)
                except Exception:
                    pass
        yield s


@contextlib.asynccontextmanager
async def aspan(name: str, attrs: Optional[dict] = None):
    """Async version — same semantics."""
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        if attrs:
            for k, v in attrs.items():
                try:
                    s.set_attribute(k, v)
                except Exception:
                    pass
        yield s


def set_attr(key: str, value: Any) -> None:
    """Attach an attribute to the current span (no-op when disabled)."""
    if not _enabled or _tracer is None:
        return
    try:
        from opentelemetry import trace
        s = trace.get_current_span()
        if s is not None:
            s.set_attribute(key, value)
    except Exception:
        pass


def add_event(name: str, attrs: Optional[dict] = None) -> None:
    """Add an event to the current span (no-op when disabled)."""
    if not _enabled or _tracer is None:
        return
    try:
        from opentelemetry import trace
        s = trace.get_current_span()
        if s is not None:
            s.add_event(name, attributes=attrs or {})
    except Exception:
        pass


def is_enabled() -> bool:
    return _enabled
