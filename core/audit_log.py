"""
Sprint 8: Append-only audit log for MCP tool calls and sensitive operations.

Stores to SQLite (WAL mode) with optional async S3 archive.
Non-blocking: writes go through an async queue so calls are never delayed.

Actions logged:
  mcp_tool_call   — LLM called an MCP tool (tool name, args, result summary)
  admin_api       — Admin endpoint called
  dnd_check       — TRAI DND check result
  consent_given   — Customer gave recording consent
  secret_rotated  — A secret key was rotated

Query at: GET /admin/audit?call_sid=xxx&action=xxx&limit=N
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./data/audit.db")


@dataclass
class AuditEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    call_sid: str = ""
    session_id: str = ""
    agent_name: str = ""
    action: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    outcome: str = "ok"       # ok | error | denied | blocked
    latency_ms: float = 0.0


class AuditLogger:
    """Singleton append-only audit logger."""

    _instance: "AuditLogger | None" = None

    def __init__(self, db_path: str = _AUDIT_DB_PATH):
        self._db_path = db_path
        self._queue: asyncio.Queue[AuditEntry] = asyncio.Queue(maxsize=10_000)
        self._writer_task: asyncio.Task | None = None
        self._db = None

    @classmethod
    def get(cls) -> "AuditLogger | None":
        return cls._instance

    @classmethod
    def set_instance(cls, instance: "AuditLogger") -> None:
        cls._instance = instance

    async def start(self) -> None:
        import aiosqlite
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                entry_id   TEXT PRIMARY KEY,
                timestamp  REAL NOT NULL,
                call_sid   TEXT,
                session_id TEXT,
                agent_name TEXT,
                action     TEXT NOT NULL,
                details    TEXT,
                outcome    TEXT,
                latency_ms REAL
            )""")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_a_call ON audit_log(call_sid)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_a_ts ON audit_log(timestamp)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_a_action ON audit_log(action)")
        await self._db.commit()
        self._writer_task = asyncio.create_task(self._writer_loop(), name="audit-writer")
        logger.info(f"[AuditLogger] Started db={self._db_path}")

    async def stop(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()
        # Drain remaining entries before closing
        while not self._queue.empty():
            try:
                entry = self._queue.get_nowait()
                await self._write(entry)
            except asyncio.QueueEmpty:
                break
        if self._db:
            await self._db.close()

    def log(
        self,
        action: str,
        *,
        call_sid: str = "",
        session_id: str = "",
        agent_name: str = "",
        details: dict | None = None,
        outcome: str = "ok",
        latency_ms: float = 0.0,
    ) -> None:
        """Fire-and-forget audit write. Never blocks."""
        entry = AuditEntry(
            call_sid=call_sid,
            session_id=session_id,
            agent_name=agent_name,
            action=action,
            details=details or {},
            outcome=outcome,
            latency_ms=latency_ms,
        )
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("[AuditLogger] Queue full — dropping entry")

    async def query(
        self,
        call_sid: str | None = None,
        action: str | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if not self._db:
            return []
        conds, params = [], []
        if call_sid:
            conds.append("call_sid = ?"); params.append(call_sid)
        if action:
            conds.append("action = ?"); params.append(action)
        if from_ts:
            conds.append("timestamp >= ?"); params.append(from_ts)
        if to_ts:
            conds.append("timestamp <= ?"); params.append(to_ts)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limit)
        async with self._db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def _writer_loop(self) -> None:
        while True:
            entry = await self._queue.get()
            await self._write(entry)
            self._queue.task_done()

    async def _write(self, entry: AuditEntry) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
                (entry.entry_id, entry.timestamp, entry.call_sid, entry.session_id,
                 entry.agent_name, entry.action, json.dumps(entry.details),
                 entry.outcome, entry.latency_ms),
            )
            await self._db.commit()
        except Exception as e:
            logger.error(f"[AuditLogger] Write error: {e}")
