"""
Outbound campaign manager.

Parses an uploaded .xlsx / .csv of leads and dials them through Asterisk AMI
at a controlled rate. Each lead's lifecycle is mirrored to a SQLite database
at `data/campaigns.db` so campaigns and per-lead status survive restarts.

Lead columns (case-insensitive, extra columns preserved under `extra`):
    customer_name, phone_number, customer_type, product_category, product_name,
    purchase_date, address, pincode, preferred_language

Backward-compat aliases (old column names still accepted):
    name → customer_name  |  mobile / phone → phone_number
    product → product_name  |  language → preferred_language
"""
from __future__ import annotations

import asyncio
import csv
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.campaign_db import CampaignDB

logger = logging.getLogger(__name__)


_REQUIRED_COLUMNS = {"customer_name", "phone_number"}

_KNOWN_COLUMNS = {
    "customer_name", "phone_number", "customer_type", "product_category",
    "product_name", "purchase_date", "address", "pincode", "preferred_language",
}

# Old column names → canonical field names
_COLUMN_ALIASES: dict[str, str] = {
    "name":     "customer_name",
    "mobile":   "phone_number",
    "phone":    "phone_number",
    "product":  "product_name",
    "language": "preferred_language",
    "call_type": "customer_type",
}


@dataclass
class Lead:
    customer_name: str
    phone_number: str
    customer_type: str = ""
    product_category: str = ""
    product_name: str = ""
    purchase_date: str = ""
    address: str = ""
    pincode: str = ""
    preferred_language: str = ""
    campaign_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # Runtime state
    status: str = "queued"        # queued | dialing | originated | failed | completed
    uuid: str = ""
    action_id: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class Campaign:
    id: str
    created_at: float
    rate_per_min: int
    leads: list[Lead]
    name: str = ""                # user-provided name (falls back to id when blank)
    campaign_type: str = ""       # NPS | Sales | Service | Real Estate | Appliances
    status: str = "running"       # running | completed | stopped
    cursor: int = 0               # next lead index to dial


# ── Parsing ──────────────────────────────────────────────────────────────────


def _normalize_row(row: dict[str, Any]) -> Lead:
    # Normalize keys: lowercase, strip whitespace, apply aliases
    raw = {str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in row.items()}
    low: dict[str, str] = {}
    for k, v in raw.items():
        canonical = _COLUMN_ALIASES.get(k, k)
        # Don't clobber a canonical value already set by its proper name
        if canonical not in low or not low[canonical]:
            low[canonical] = v

    missing = _REQUIRED_COLUMNS - {k for k, v in low.items() if v}
    if missing:
        raise ValueError(f"lead row missing required columns: {sorted(missing)} (row={row})")

    extra = {k: v for k, v in low.items() if k not in _KNOWN_COLUMNS and v}
    return Lead(
        customer_name=low.get("customer_name", ""),
        phone_number=low.get("phone_number", ""),
        customer_type=low.get("customer_type", ""),
        product_category=low.get("product_category", ""),
        product_name=low.get("product_name", ""),
        purchase_date=low.get("purchase_date", ""),
        address=low.get("address", ""),
        pincode=low.get("pincode", ""),
        preferred_language=low.get("preferred_language", ""),
        extra=extra,
    )


def parse_file(path: str | Path) -> list[Lead]:
    """Parse a .csv or .xlsx file of leads. Raises ValueError on required-column misses."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"leads file not found: {p}")
    ext = p.suffix.lower()
    if ext == ".csv":
        return _parse_csv(p)
    if ext in (".xlsx", ".xlsm"):
        return _parse_xlsx(p)
    raise ValueError(f"unsupported leads file type: {ext}")


def _parse_csv(p: Path) -> list[Lead]:
    leads: list[Lead] = []
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            try:
                leads.append(_normalize_row(row))
            except ValueError as e:
                logger.warning(f"csv row {i} skipped: {e}")
    return leads


def _parse_xlsx(p: Path) -> list[Lead]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise RuntimeError("openpyxl is required to parse .xlsx; pip install openpyxl") from e
    wb = load_workbook(filename=str(p), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        return []
    header = [str(h).strip() if h else "" for h in header]
    leads: list[Lead] = []
    for i, row in enumerate(rows, start=2):
        if row is None or all(c in (None, "") for c in row):
            continue
        record = {header[j]: row[j] for j in range(len(header)) if header[j]}
        try:
            leads.append(_normalize_row(record))
        except ValueError as e:
            logger.warning(f"xlsx row {i} skipped: {e}")
    return leads


# ── Campaign manager ─────────────────────────────────────────────────────────


class CampaignManager:
    """
    Owns the dial loop for all active campaigns. Single instance per process.

    The `dial_fn` callback is invoked as `await dial_fn(lead) -> dict` and is
    expected to return a dict with keys `uuid`, `action_id`, and `status`
    (e.g. "originated") or raise on failure.

    Persistence: campaigns + per-lead state are mirrored to SQLite at
    `data/campaigns.db`. On startup, `_rehydrate()` reloads every campaign,
    marks any non-terminal leads (dialing / originated / in_progress) as
    failed with reason "interrupted by server restart", and resumes any
    `running` campaigns from their persisted cursor.
    """

    def __init__(self, dial_fn, db_path: str | Path = "data/campaigns.db"):
        self._dial_fn = dial_fn
        self._campaigns: dict[str, Campaign] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._db = CampaignDB(db_path)
        # Map call-uuid → (campaign_id, seq) for fast pipeline-hook updates.
        self._uuid_index: dict[str, tuple[str, int]] = {}
        self._rehydrate()

    # ── Public API ──────────────────────────────────────────────────────────

    def create(
        self,
        leads: list[Lead],
        rate_per_min: int = 6,
        name: str = "",
        campaign_type: str = "",
    ) -> Campaign:
        if rate_per_min < 1:
            rate_per_min = 1
        campaign = Campaign(
            id=str(uuid.uuid4())[:8],
            name=(name or "").strip(),
            campaign_type=(campaign_type or "").strip(),
            created_at=time.time(),
            rate_per_min=rate_per_min,
            leads=leads,
        )
        self._db.insert_campaign(
            campaign.id, campaign.created_at, rate_per_min, leads,
            name=campaign.name, campaign_type=campaign.campaign_type,
        )
        self._campaigns[campaign.id] = campaign
        self._tasks[campaign.id] = asyncio.create_task(
            self._run(campaign), name=f"campaign-{campaign.id}"
        )
        label = campaign.name or campaign.id
        logger.info(
            f"[campaign {campaign.id} \"{label}\"] started: {len(leads)} leads at {rate_per_min}/min"
        )
        return campaign

    def get(self, campaign_id: str) -> Campaign | None:
        return self._campaigns.get(campaign_id)

    def list_campaigns(self) -> list[dict]:
        return [self._snapshot(c) for c in self._campaigns.values()]

    def snapshot(self, campaign_id: str) -> dict | None:
        c = self._campaigns.get(campaign_id)
        return self._snapshot(c) if c else None

    def stop(self, campaign_id: str) -> bool:
        c = self._campaigns.get(campaign_id)
        if not c or c.status != "running":
            return False
        c.status = "stopped"
        task = self._tasks.get(campaign_id)
        if task and not task.done():
            task.cancel()
        self._db.update_campaign(campaign_id, status="stopped")
        return True

    async def mark_in_progress(self, call_uuid: str) -> None:
        """Pipeline hook: customer answered, AudioSocket connected."""
        if not call_uuid:
            return
        loc = self._uuid_index.get(call_uuid)
        await self._db.aupdate_lead_by_uuid(call_uuid, status="in_progress")
        if loc:
            cid, seq = loc
            c = self._campaigns.get(cid)
            if c and 0 <= seq < len(c.leads):
                c.leads[seq].status = "in_progress"

    async def mark_completed(
        self,
        call_uuid: str,
        success: bool,
        reason: str = "",
        mobile: str = "",
    ) -> None:
        """
        Pipeline hook: call ended (cleanly or with failure).

        ``mobile`` is an optional fallback lookup key — used when the dialplan
        didn't propagate AUDIOSOCKET_UUID and the on-disk call_sid doesn't
        match any lead.uuid. We pick the most recently dialed lead with the
        matching mobile and re-link its uuid to ``call_uuid``.
        """
        if not call_uuid:
            return
        new_status = "completed" if success else "failed"
        finished_at = time.time()
        await self._db.aupdate_lead_by_uuid(
            call_uuid,
            status=new_status,
            error=reason if not success else "",
            finished_at=finished_at,
        )
        loc = self._uuid_index.get(call_uuid)

        # Fallback: AudioSocket UUID mismatch — find by mobile + recency
        if not loc and mobile:
            mobile_norm = re.sub(r"\D", "", str(mobile or ""))[-10:]
            best = None
            best_started = -1.0
            for campaign in self._campaigns.values():
                for seq, lead in enumerate(campaign.leads):
                    lead_mobile = re.sub(r"\D", "", lead.phone_number or "")[-10:]
                    if lead_mobile != mobile_norm:
                        continue
                    if lead.started_at > best_started:
                        best = (campaign.id, seq, lead.uuid)
                        best_started = lead.started_at
            if best:
                cid, seq, old_uuid = best
                # Re-link the lead to the actual call_sid
                self._uuid_index.pop(old_uuid, None)
                self._uuid_index[call_uuid] = (cid, seq)
                c = self._campaigns.get(cid)
                if c and 0 <= seq < len(c.leads):
                    c.leads[seq].uuid = call_uuid
                loc = (cid, seq)
                logger.info(
                    f"[{call_uuid}] mark_completed: relinked lead by mobile "
                    f"(old uuid={old_uuid})"
                )

        if loc:
            cid, seq = loc
            c = self._campaigns.get(cid)
            if c and 0 <= seq < len(c.leads):
                lead = c.leads[seq]
                lead.status = new_status
                lead.error = reason if not success else ""
                lead.finished_at = finished_at

    # ── Internals ───────────────────────────────────────────────────────────

    async def _run(self, campaign: Campaign) -> None:
        interval = 60.0 / max(campaign.rate_per_min, 1)
        try:
            while campaign.cursor < len(campaign.leads) and campaign.status == "running":
                lead = campaign.leads[campaign.cursor]
                seq = campaign.cursor
                lead.campaign_name = campaign.name
                lead.status = "dialing"
                lead.started_at = time.time()
                await self._db.aupdate_lead(
                    campaign.id, seq,
                    status=lead.status,
                    started_at=lead.started_at,
                )
                try:
                    result = await self._dial_fn(lead)
                    logger.info(
                        f"[campaign {campaign.id}] dial_fn result for {lead.phone_number}: {result}"
                    )
                    lead.uuid = result.get("uuid", "") or lead.uuid
                    lead.action_id = result.get("action_id", "") or lead.action_id
                    lead.status = result.get("status", "originated") or "originated"
                    if lead.uuid:
                        self._uuid_index[lead.uuid] = (campaign.id, seq)
                except Exception as e:
                    lead.status = "failed"
                    lead.error = str(e)
                    logger.warning(
                        f"[campaign {campaign.id}] lead {lead.phone_number} failed: {e!r}",
                        exc_info=True,
                    )
                lead.finished_at = time.time()
                await self._db.aupdate_lead(
                    campaign.id, seq,
                    status=lead.status,
                    uuid=lead.uuid,
                    action_id=lead.action_id,
                    error=lead.error,
                    finished_at=lead.finished_at,
                )
                campaign.cursor += 1
                await self._db.aupdate_campaign(campaign.id, cursor=campaign.cursor)

                if campaign.cursor < len(campaign.leads):
                    await asyncio.sleep(interval)
            if campaign.status == "running":
                campaign.status = "completed"
                await self._db.aupdate_campaign(campaign.id, status="completed")
        except asyncio.CancelledError:
            campaign.status = "stopped"
            await self._db.aupdate_campaign(campaign.id, status="stopped")
            raise
        finally:
            logger.info(f"[campaign {campaign.id}] finished: status={campaign.status}")

    def _snapshot(self, c: Campaign) -> dict:
        counts = {"queued": 0, "dialing": 0, "originated": 0,
                  "in_progress": 0, "completed": 0, "failed": 0}
        for lead in c.leads:
            counts[lead.status] = counts.get(lead.status, 0) + 1

        leads_out = []
        for lead in c.leads:
            d = asdict(lead)
            # Backward-compat aliases for the existing dashboard JS that reads
            # l.name / l.mobile / l.product / l.issue / l.call_type / l.language.
            d["name"] = lead.customer_name
            d["mobile"] = lead.phone_number
            d["product"] = lead.product_name
            d["call_type"] = lead.customer_type
            d["language"] = lead.preferred_language
            leads_out.append(d)

        return {
            "id": c.id,
            "name": c.name,
            "campaign_type": c.campaign_type,
            "created_at": c.created_at,
            "rate_per_min": c.rate_per_min,
            "status": c.status,
            "cursor": c.cursor,
            "total": len(c.leads),
            "counts": counts,
            "leads": leads_out,
        }

    # ── Rehydration on startup ──────────────────────────────────────────────

    def _rehydrate(self) -> None:
        """Load campaigns + leads from DB; reset orphans; resume running ones."""
        orphans = self._db.mark_orphans_failed()
        if orphans:
            logger.warning(
                f"Rehydrate: {orphans} orphan lead(s) (dialing / in_progress) "
                f"marked failed due to prior server interruption"
            )

        rows = self._db.load_all_campaigns()
        for entry in rows:
            crow = entry["campaign"]
            leads_rows = entry["leads"]

            # Build Lead dataclass instances from rows.
            leads: list[Lead] = []
            for lr in leads_rows:
                leads.append(Lead(
                    customer_name=lr.get("customer_name", "") or "",
                    phone_number=lr.get("phone_number", "") or "",
                    customer_type=lr.get("customer_type", "") or "",
                    product_category=lr.get("product_category", "") or "",
                    product_name=lr.get("product_name", "") or "",
                    purchase_date=lr.get("purchase_date", "") or "",
                    address=lr.get("address", "") or "",
                    pincode=lr.get("pincode", "") or "",
                    preferred_language=lr.get("preferred_language", "") or "",
                    extra=lr.get("extra") or {},
                    status=lr.get("status", "queued") or "queued",
                    uuid=lr.get("uuid", "") or "",
                    action_id=lr.get("action_id", "") or "",
                    error=lr.get("error", "") or "",
                    started_at=float(lr.get("started_at") or 0.0),
                    finished_at=float(lr.get("finished_at") or 0.0),
                ))

            campaign = Campaign(
                id=crow["id"],
                name=crow.get("name", "") or "",
                campaign_type=crow.get("campaign_type", "") or "",
                created_at=float(crow["created_at"]),
                rate_per_min=int(crow["rate_per_min"]),
                leads=leads,
                status=crow["status"],
                cursor=int(crow["cursor"]),
            )
            self._campaigns[campaign.id] = campaign

            # Rebuild uuid index from terminal-but-known rows so a late
            # pipeline hook (mark_completed) can still update them if needed.
            for seq, lead in enumerate(campaign.leads):
                if lead.uuid:
                    self._uuid_index[lead.uuid] = (campaign.id, seq)

        # Resume any campaigns left in 'running' state that still have queued leads.
        for campaign in self._campaigns.values():
            has_pending = campaign.cursor < len(campaign.leads)
            if campaign.status == "running" and has_pending:
                logger.info(
                    f"[campaign {campaign.id}] resuming from cursor={campaign.cursor} "
                    f"(of {len(campaign.leads)} leads)"
                )
                try:
                    self._tasks[campaign.id] = asyncio.create_task(
                        self._run(campaign), name=f"campaign-{campaign.id}"
                    )
                except RuntimeError:
                    # Instantiated outside a running loop (e.g., tests). Caller
                    # can call `resume_running()` once a loop is available.
                    logger.warning(
                        f"[campaign {campaign.id}] no running event loop — "
                        "skipping auto-resume"
                    )
            elif campaign.status == "running" and not has_pending:
                # Was running, no leads left → mark completed.
                campaign.status = "completed"
                self._db.update_campaign(campaign.id, status="completed")

        if self._campaigns:
            logger.info(f"Rehydrated {len(self._campaigns)} campaign(s) from SQLite")


# ── Module-level singleton (initialized by main.py once dial_fn is wired) ───


_manager: CampaignManager | None = None


def set_manager(manager: CampaignManager) -> None:
    global _manager
    _manager = manager


def get_manager() -> CampaignManager | None:
    return _manager
