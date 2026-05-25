"""
Sprint 6: Call Outcome Store — persists per-call analytics records.

Backends
--------
  sqlite  — async SQLite via aiosqlite (default, zero infra needed)
  s3      — JSON records in S3/compatible bucket (requires boto3)
  none    — disabled (NullOutcomeStore)

Each completed call writes one CallRecord containing outcome, language,
turn count, agent path, and timing — forming the source of truth for the
Sprint 6 analytics and ops dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CallRecord:
    call_sid: str
    started_at: float            # unix timestamp (time.time() at call connect)
    ended_at: float              # unix timestamp (time.time() at call end)
    duration_s: float            # ended_at - started_at
    outcome: str                 # resolved | abandoned | max_duration | error | unknown
    language: str                # final detected language code (e.g. "hi", "en")
    total_turns: int             # number of user → agent exchanges
    agent_path: list[str]        # sequence of agent names (e.g. ["screener", "service", "closer"])
    handoff_count: int           # len(agent_path) - 1 (number of handoffs)
    barge_in_count: int          # confirmed barge-ins during this call
    did: str = ""                # destination number / DID if available
    error_detail: str = ""       # error message if outcome == "error"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at_iso"] = datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat()
        d["ended_at_iso"]   = datetime.fromtimestamp(self.ended_at,   tz=timezone.utc).isoformat()
        return d


# ── Abstract base ─────────────────────────────────────────────────────────────

class BaseOutcomeStore(ABC):
    @abstractmethod
    async def save(self, record: CallRecord) -> None:
        """Persist a completed call record."""

    @abstractmethod
    async def query_recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent `limit` records as dicts (newest first)."""

    @abstractmethod
    async def query_since(self, since_ts: float) -> list[dict]:
        """Return all records with started_at >= since_ts as dicts."""

    async def close(self) -> None:
        """Optional cleanup — override in backends that hold connections."""


# ── Null (disabled) ───────────────────────────────────────────────────────────

class NullOutcomeStore(BaseOutcomeStore):
    async def save(self, record: CallRecord) -> None:
        pass

    async def query_recent(self, limit: int = 100) -> list[dict]:
        return []

    async def query_since(self, since_ts: float) -> list[dict]:
        return []


# ── SQLite backend ────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    call_sid       TEXT PRIMARY KEY,
    started_at     REAL NOT NULL,
    ended_at       REAL NOT NULL,
    duration_s     REAL NOT NULL,
    outcome        TEXT NOT NULL,
    language       TEXT NOT NULL DEFAULT 'unknown',
    total_turns    INTEGER NOT NULL DEFAULT 0,
    agent_path     TEXT NOT NULL DEFAULT '[]',
    handoff_count  INTEGER NOT NULL DEFAULT 0,
    barge_in_count INTEGER NOT NULL DEFAULT 0,
    did            TEXT NOT NULL DEFAULT '',
    error_detail   TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_IDX_STARTED = """
CREATE INDEX IF NOT EXISTS idx_calls_started_at ON calls (started_at DESC);
"""

_INSERT = """
INSERT OR REPLACE INTO calls
    (call_sid, started_at, ended_at, duration_s, outcome, language,
     total_turns, agent_path, handoff_count, barge_in_count, did, error_detail)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?);
"""


class SQLiteOutcomeStore(BaseOutcomeStore):
    """
    Async SQLite-backed call outcome store using aiosqlite.

    All writes are non-blocking (aiosqlite runs SQLite in a thread pool).
    Suitable for single-node / dev deployments and moderate call volumes.

    For 50K+ calls/day in production, use S3OutcomeStore or migrate to
    a proper time-series DB (TimescaleDB, ClickHouse).
    """

    def __init__(self, db_path: str = "./data/calls.db"):
        self._db_path = db_path
        self._db = None   # aiosqlite.Connection — opened lazily

    async def _conn(self):
        """Return the open aiosqlite connection, opening it if needed."""
        if self._db is None:
            try:
                import aiosqlite
            except ImportError:
                raise RuntimeError(
                    "aiosqlite is required for SQLiteOutcomeStore. "
                    "Install it: pip install aiosqlite>=0.19.0"
                )
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            # WAL mode: allows concurrent readers while one writer is active
            await self._db.execute("PRAGMA journal_mode=WAL;")
            await self._db.execute(_CREATE_TABLE)
            await self._db.execute(_CREATE_IDX_STARTED)
            await self._db.commit()
            logger.info(f"SQLiteOutcomeStore: opened {self._db_path}")
        return self._db

    async def save(self, record: CallRecord) -> None:
        try:
            db = await self._conn()
            await db.execute(_INSERT, (
                record.call_sid,
                record.started_at,
                record.ended_at,
                record.duration_s,
                record.outcome,
                record.language,
                record.total_turns,
                json.dumps(record.agent_path),
                record.handoff_count,
                record.barge_in_count,
                record.did,
                record.error_detail,
            ))
            await db.commit()
            logger.debug(f"CallRecord saved: {record.call_sid} outcome={record.outcome}")
        except Exception:
            logger.exception(f"CallRecord save failed for {record.call_sid}")

    async def query_recent(self, limit: int = 100) -> list[dict]:
        try:
            db = await self._conn()
            async with db.execute(
                "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]
        except Exception:
            logger.exception("query_recent failed")
            return []

    async def query_since(self, since_ts: float) -> list[dict]:
        try:
            db = await self._conn()
            async with db.execute(
                "SELECT * FROM calls WHERE started_at >= ? ORDER BY started_at DESC",
                (since_ts,),
            ) as cursor:
                rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]
        except Exception:
            logger.exception("query_since failed")
            return []

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("SQLiteOutcomeStore: closed")


def _row_to_dict(row) -> dict:
    d = dict(row)
    # Decode agent_path JSON string back to list
    try:
        d["agent_path"] = json.loads(d.get("agent_path", "[]"))
    except Exception:
        d["agent_path"] = []
    # Add ISO timestamps for display
    d["started_at_iso"] = datetime.fromtimestamp(d["started_at"], tz=timezone.utc).isoformat()
    d["ended_at_iso"]   = datetime.fromtimestamp(d["ended_at"],   tz=timezone.utc).isoformat()
    return d


# ── S3 backend (optional — requires boto3) ────────────────────────────────────

class S3OutcomeStore(BaseOutcomeStore):
    """
    Writes each call record as a JSON file to S3 (or any S3-compatible store).

    Key format: {prefix}{YYYY/MM/DD}/{call_sid}.json

    Requires: pip install boto3
    Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION (or
    use instance roles in EC2/ECS).
    """

    def __init__(self, bucket: str, prefix: str = "calls/", region: str = ""):
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        self._region = region or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

    def _key(self, record: CallRecord) -> str:
        dt = datetime.fromtimestamp(record.started_at, tz=timezone.utc)
        return f"{self._prefix}{dt.strftime('%Y/%m/%d')}/{record.call_sid}.json"

    async def save(self, record: CallRecord) -> None:
        import asyncio
        try:
            import boto3
        except ImportError:
            logger.error("boto3 is required for S3OutcomeStore: pip install boto3")
            return

        key = self._key(record)
        body = json.dumps(record.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")

        def _put():
            s3 = boto3.client("s3", region_name=self._region)
            s3.put_object(Bucket=self._bucket, Key=key, Body=body,
                          ContentType="application/json")

        try:
            await asyncio.get_event_loop().run_in_executor(None, _put)
            logger.debug(f"S3OutcomeStore: saved s3://{self._bucket}/{key}")
        except Exception:
            logger.exception(f"S3OutcomeStore: save failed for {record.call_sid}")

    async def query_recent(self, limit: int = 100) -> list[dict]:
        # S3 is write-optimised — analytics queries should use Athena/Glue.
        # For the ops dashboard, we rely on a local SQLite cache alongside S3.
        logger.warning("S3OutcomeStore.query_recent: not supported — use Athena or add a local SQLite cache")
        return []

    async def query_since(self, since_ts: float) -> list[dict]:
        logger.warning("S3OutcomeStore.query_since: not supported — use Athena or add a local SQLite cache")
        return []


# ── Factory ───────────────────────────────────────────────────────────────────

def create_outcome_store(
    backend: str = "sqlite",
    db_path: str = "./data/calls.db",
    s3_bucket: str = "",
    s3_prefix: str = "calls/",
) -> BaseOutcomeStore:
    """
    Create and return the appropriate outcome store backend.

    backend:
      "sqlite"  — SQLiteOutcomeStore (default)
      "s3"      — S3OutcomeStore (requires s3_bucket)
      "none"    — NullOutcomeStore (disabled)
    """
    backend = backend.lower()
    if backend == "sqlite":
        logger.info(f"CallOutcomeStore: SQLite → {db_path}")
        return SQLiteOutcomeStore(db_path=db_path)
    elif backend == "s3":
        if not s3_bucket:
            logger.error("OUTCOME_S3_BUCKET not set — falling back to NullOutcomeStore")
            return NullOutcomeStore()
        logger.info(f"CallOutcomeStore: S3 → s3://{s3_bucket}/{s3_prefix}")
        return S3OutcomeStore(bucket=s3_bucket, prefix=s3_prefix)
    else:
        logger.info("CallOutcomeStore: disabled (backend=none)")
        return NullOutcomeStore()
