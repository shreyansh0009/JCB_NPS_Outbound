"""
Sprint 4: Structured JSON logging.

Every log record is emitted as a single JSON object on stdout.
Compatible with ELK, CloudWatch, Datadog, Loki, and any log aggregation system.

Features:
  - call_sid field on every record (set via ContextVar — zero code changes in callers)
  - component field extracted from logger name
  - duration_ms included when present in log args
  - Stack traces on ERROR/CRITICAL preserved in "exc_info" field
  - Human-readable fallback when running locally (set STRUCTURED_LOGS=false)

Usage:
    from core.structured_logger import setup_structured_logging, set_call_context

    # At startup:
    setup_structured_logging(level="INFO")

    # At call start (sets call_sid on all logs from this coroutine):
    set_call_context(call_sid="abc-123")

    # Normal logging just works:
    logger = logging.getLogger("streaming_pipeline")
    logger.info("turn started", extra={"duration_ms": 142.3, "agent": "service"})
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# ── Per-coroutine call context ────────────────────────────────────────────────
# Set via ContextVar so every log line in a coroutine automatically carries
# the call_sid without being explicitly passed around.

_call_sid_var: ContextVar[str] = ContextVar("call_sid", default="")
_agent_var:    ContextVar[str] = ContextVar("agent",    default="")


def set_call_context(call_sid: str = "", agent: str = "") -> None:
    """
    Set the call_sid (and optionally current agent) for the current asyncio task.
    All log lines emitted from this coroutine tree will include these fields.
    """
    _call_sid_var.set(call_sid)
    if agent:
        _agent_var.set(agent)


def clear_call_context() -> None:
    _call_sid_var.set("")
    _agent_var.set("")


# ── JSON formatter ────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """
    Formats every log record as a single JSON line.

    Standard fields in every record:
        ts          ISO-8601 timestamp (UTC)
        level       DEBUG / INFO / WARNING / ERROR / CRITICAL
        logger      Logger name (e.g. "streaming_pipeline", "core.agent")
        msg         Log message
        call_sid    Active call UUID (from ContextVar, empty if not in a call)
        agent       Active agent name (from ContextVar, if set)

    Extra fields passed via logging.extra / exc_info / stack_info are merged in.
    """

    _LEVEL_MAP = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts":       datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":    self._LEVEL_MAP.get(record.levelno, record.levelname),
            "logger":   record.name,
            "msg":      record.getMessage(),
            "call_sid": _call_sid_var.get(""),
            "agent":    _agent_var.get(""),
        }

        # Extra fields from logger.info("msg", extra={...})
        _SKIP = frozenset({
            "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "name", "message", "taskName",
        })
        for key, val in record.__dict__.items():
            if key not in _SKIP and not key.startswith("_"):
                obj[key] = val

        # Exception info
        if record.exc_info and record.exc_info[0] is not None:
            obj["exc_type"]  = record.exc_info[0].__name__
            obj["exc_value"] = str(record.exc_info[1])
            obj["traceback"] = traceback.format_exception(*record.exc_info)

        # Remove empty call_sid / agent to keep output clean
        if not obj["call_sid"]:
            del obj["call_sid"]
        if not obj["agent"]:
            del obj["agent"]

        try:
            return json.dumps(obj, default=str, ensure_ascii=False)
        except Exception:
            # Never crash the application due to a logging error
            return json.dumps({"ts": obj["ts"], "level": "ERROR",
                               "msg": "Failed to serialize log record",
                               "raw": str(record.getMessage())})


# ── Human-readable formatter (local dev) ─────────────────────────────────────

class DevFormatter(logging.Formatter):
    """Coloured human-readable format for local development."""
    _COLOURS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        colour = self._COLOURS.get(level, "")
        call_sid = _call_sid_var.get("")
        prefix = f"[{call_sid[:8]}] " if call_sid else ""
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        base = f"{ts} {colour}{level:<8}{self._RESET} {record.name:<30} {prefix}{record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


# ── PII masking ───────────────────────────────────────────────────────────────

# Matches Indian mobile numbers: 10 digits starting with 6-9, optional +91/0 prefix
_PHONE_RE   = re.compile(r'\b(\+91|0)?[6-9]\d{9}\b')
# Matches Aadhaar numbers: 12 digits optionally separated by spaces or hyphens
_AADHAAR_RE = re.compile(r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b')


class PIIMaskingFilter(logging.Filter):
    """
    Logging filter that masks PII (phone numbers, Aadhaar) before records are emitted.

    Applied at the handler level in setup_structured_logging() so it runs regardless
    of which formatter is active. Modifies record.msg in place — args are cleared so
    %-formatting never re-exposes the raw value.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            msg = _PHONE_RE.sub("[PHONE]", msg)
            msg = _AADHAAR_RE.sub("[AADHAAR]", msg)
            record.msg  = msg
            record.args = ()   # prevent re-formatting from restoring original values
        except Exception:
            pass  # never block a log line due to masking failure
        return True


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_structured_logging(
    level: str = "INFO",
    structured: Optional[bool] = None,
) -> None:
    """
    Configure root logger with structured (JSON) or dev (human) formatter.

    Args:
        level:      Log level string ("DEBUG", "INFO", "WARNING", "ERROR")
        structured: True = always JSON; False = always dev; None = auto
                    (auto: JSON in production, dev if STRUCTURED_LOGS=false)
    """
    if structured is None:
        env_val = os.getenv("STRUCTURED_LOGS", "").lower()
        structured = env_val not in ("false", "0", "no", "")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if structured else DevFormatter())
    
    # Allow disabling PII masking for local development
    mask_pii = os.getenv("MASK_PII", "true").lower() not in ("false", "0", "no")
    if mask_pii:
        handler.addFilter(PIIMaskingFilter())

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    # Remove any existing handlers (avoid duplicate output)
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Quiet down noisy libraries
    for noisy in ("uvicorn.access", "httpx", "httpcore", "websockets.client",
                  "chromadb", "sentence_transformers", "torch"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    mode = "JSON (structured)" if structured else "human-readable (dev)"
    logging.getLogger(__name__).info(
        f"Logging initialised: level={level} format={mode}"
    )
