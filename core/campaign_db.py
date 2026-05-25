"""
SQLite persistence for outbound campaigns and leads.

A single file at `data/campaigns.db` holds both the campaign list and every
lead's lifecycle. Designed for single-server use:
  - WAL mode for concurrent readers + serialised writers
  - One Python-side lock so async tasks don't trample each other's transactions
  - Targeted UPDATEs on lead status changes (no whole-campaign rewrites)

The schema mirrors the in-memory `Lead` / `Campaign` dataclasses in
`core.outbound_campaign`. The `extra` dict is JSON-encoded; everything else
maps 1:1 to a column.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL DEFAULT '',
    campaign_type   TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    rate_per_min    INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    cursor          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_campaigns_created_at ON campaigns(created_at);
CREATE INDEX IF NOT EXISTS idx_campaigns_status     ON campaigns(status);

CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         TEXT    NOT NULL,
    seq                 INTEGER NOT NULL,
    customer_name       TEXT    NOT NULL DEFAULT '',
    phone_number        TEXT    NOT NULL DEFAULT '',
    customer_type       TEXT    NOT NULL DEFAULT '',
    product_category    TEXT    NOT NULL DEFAULT '',
    product_name        TEXT    NOT NULL DEFAULT '',
    purchase_date       TEXT    NOT NULL DEFAULT '',
    address             TEXT    NOT NULL DEFAULT '',
    pincode             TEXT    NOT NULL DEFAULT '',
    preferred_language  TEXT    NOT NULL DEFAULT '',
    extra               TEXT    NOT NULL DEFAULT '{}',
    status              TEXT    NOT NULL DEFAULT 'queued',
    uuid                TEXT    NOT NULL DEFAULT '',
    action_id           TEXT    NOT NULL DEFAULT '',
    error               TEXT    NOT NULL DEFAULT '',
    started_at          REAL    NOT NULL DEFAULT 0,
    finished_at         REAL    NOT NULL DEFAULT 0,
    UNIQUE(campaign_id, seq),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_leads_campaign_id ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_status      ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_uuid        ON leads(uuid);
"""


# Columns we copy 1:1 between Python dataclass and SQLite row.
_LEAD_COLS: tuple[str, ...] = (
    "customer_name", "phone_number", "customer_type", "product_category",
    "product_name", "purchase_date", "address", "pincode", "preferred_language",
    "status", "uuid", "action_id", "error", "started_at", "finished_at",
)


class CampaignDB:
    """Thread-safe SQLite repository for campaigns and leads."""

    def __init__(self, db_path: str | Path = "data/campaigns.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the same connection can be reused from
        # the asyncio event loop and any pool threads — guarded by self._lock.
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA_SQL)
            # Migrations for pre-existing campaigns table.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(campaigns)").fetchall()}
            if "name" not in cols:
                self._conn.execute("ALTER TABLE campaigns ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                logger.info("CampaignDB: added 'name' column to campaigns table")
            if "campaign_type" not in cols:
                self._conn.execute("ALTER TABLE campaigns ADD COLUMN campaign_type TEXT NOT NULL DEFAULT ''")
                logger.info("CampaignDB: added 'campaign_type' column to campaigns table")
        logger.info(f"CampaignDB initialised at {self._db_path}")

    # ── Inserts ───────────────────────────────────────────────────────────

    def insert_campaign(
        self,
        campaign_id: str,
        created_at: float,
        rate_per_min: int,
        leads,
        name: str = "",
        campaign_type: str = "",
    ) -> None:
        """Atomically insert a campaign and all of its leads."""
        rows = [
            (
                campaign_id, idx,
                lead.customer_name, lead.phone_number, lead.customer_type,
                lead.product_category, lead.product_name, lead.purchase_date,
                lead.address, lead.pincode, lead.preferred_language,
                json.dumps(lead.extra or {}, ensure_ascii=False),
                lead.status, lead.uuid, lead.action_id, lead.error,
                lead.started_at, lead.finished_at,
            )
            for idx, lead in enumerate(leads)
        ]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO campaigns(id, name, campaign_type, created_at, rate_per_min, status, cursor) "
                "VALUES (?, ?, ?, ?, ?, 'running', 0)",
                (campaign_id, name or "", campaign_type or "", created_at, rate_per_min),
            )
            if rows:
                self._conn.executemany(
                    """
                    INSERT INTO leads(
                        campaign_id, seq,
                        customer_name, phone_number, customer_type,
                        product_category, product_name, purchase_date,
                        address, pincode, preferred_language,
                        extra,
                        status, uuid, action_id, error, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    # ── Updates ───────────────────────────────────────────────────────────

    def update_campaign(self, campaign_id: str, **fields: Any) -> None:
        """Update one or more campaign columns. Allowed: status, cursor, rate_per_min."""
        allowed = {"status", "cursor", "rate_per_min"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        params = [fields[k] for k in cols] + [campaign_id]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                params,
            )

    def update_lead(self, campaign_id: str, seq: int, **fields: Any) -> None:
        """Update one or more columns of a specific lead by (campaign_id, seq)."""
        cols = [k for k in fields if k in _LEAD_COLS]
        if not cols:
            return
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        params = [fields[k] for k in cols] + [campaign_id, seq]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE leads SET {set_clause} WHERE campaign_id = ? AND seq = ?",
                params,
            )

    def update_lead_by_uuid(self, lead_uuid: str, **fields: Any) -> tuple[str, int] | None:
        """
        Update a lead identified by call UUID (used by pipeline lifecycle hooks).
        Returns (campaign_id, seq) if a row was updated, else None.
        """
        if not lead_uuid:
            return None
        cols = [k for k in fields if k in _LEAD_COLS]
        if not cols:
            return None
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        params = [fields[k] for k in cols] + [lead_uuid]
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE leads SET {set_clause} WHERE uuid = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT campaign_id, seq FROM leads WHERE uuid = ?",
                (lead_uuid,),
            ).fetchone()
            return (row["campaign_id"], row["seq"]) if row else None

    def mark_orphans_failed(self, reason: str = "interrupted by server restart") -> int:
        """
        Any lead in a transient mid-call state when the server died is marked
        failed. Returns count of rows touched.

        We only sweep `dialing` (AMI request mid-flight) and `in_progress`
        (AudioSocket was live). `originated` is left alone — it represents
        "AMI accepted the call" and, until pipeline lifecycle hooks are wired
        in, is the natural terminal "success" state for completed calls.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE leads
                   SET status = 'failed',
                       error  = ?
                 WHERE status IN ('dialing', 'in_progress')
                """,
                (reason,),
            )
            return cur.rowcount

    # ── Reads ─────────────────────────────────────────────────────────────

    def load_all_campaigns(self) -> list[dict]:
        """
        Returns a list of {campaign_row, leads_rows} for rehydration on startup.
        Each lead row is a plain dict with all columns + parsed `extra`.
        """
        with self._lock:
            campaigns = self._conn.execute(
                "SELECT * FROM campaigns ORDER BY created_at ASC"
            ).fetchall()
            out: list[dict] = []
            for c in campaigns:
                leads = self._conn.execute(
                    "SELECT * FROM leads WHERE campaign_id = ? ORDER BY seq ASC",
                    (c["id"],),
                ).fetchall()
                out.append({
                    "campaign": dict(c),
                    "leads": [self._row_to_lead_dict(r) for r in leads],
                })
            return out

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self._lock:
            c = self._conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if not c:
                return None
            leads = self._conn.execute(
                "SELECT * FROM leads WHERE campaign_id = ? ORDER BY seq ASC",
                (campaign_id,),
            ).fetchall()
            return {
                "campaign": dict(c),
                "leads": [self._row_to_lead_dict(r) for r in leads],
            }

    @staticmethod
    def _row_to_lead_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except json.JSONDecodeError:
            d["extra"] = {}
        return d

    # ── Async wrappers (hot path) ─────────────────────────────────────────
    # These offload the sync sqlite3 calls to a worker thread so the event
    # loop is never blocked by WAL fsync or the threading.Lock contention
    # under bursty load. Sync variants stay for startup paths and tests.

    async def aupdate_lead(self, campaign_id: str, seq: int, **fields: Any) -> None:
        await asyncio.to_thread(self.update_lead, campaign_id, seq, **fields)

    async def aupdate_lead_by_uuid(self, lead_uuid: str, **fields: Any) -> tuple[str, int] | None:
        return await asyncio.to_thread(self.update_lead_by_uuid, lead_uuid, **fields)

    async def aupdate_campaign(self, campaign_id: str, **fields: Any) -> None:
        await asyncio.to_thread(self.update_campaign, campaign_id, **fields)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()
