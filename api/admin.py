"""
Admin API: health checks and outbound-calling endpoints.

Outbound endpoints
------------------
  POST /api/outbound/originate                - dial a single lead
  POST /api/outbound/campaign/upload          - upload xlsx/csv, start batch campaign
  GET  /api/outbound/campaigns                - list all campaigns
  GET  /api/outbound/campaign/{campaign_id}   - campaign status snapshot
  POST /api/outbound/campaign/{id}/stop       - stop a running campaign
  GET  /admin/outbound                        - HTML dashboard
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import uuid as _uuid
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config.settings import Settings
from core import outbound_call_store
from core.ami import get_ami
from core.outbound_campaign import Lead, get_manager, parse_file
from core.rate_limiter import TenantRateLimiter
from core.trunk_pool import TrunkPool, get_pool as get_trunk_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["admin"])


# ── Request models ──────────────────────────────────────────────────────────


class OriginateRequest(BaseModel):
    customer_name: str
    phone_number: str
    customer_type: str = ""
    product_category: str = ""
    product_name: str = ""
    purchase_date: str = ""
    address: str = ""
    pincode: str = ""
    preferred_language: str = ""


class TranslateRequest(BaseModel):
    transcript: list[dict]
    language: str = "unknown"


# ── Health ──────────────────────────────────────────────────────────────────


@router.get("/admin/health")
async def health():
    return {"status": "ok"}


# ── Prompt hot-reload ───────────────────────────────────────────────────────
#
# Forces every agent's `system_prompt` to be re-read from disk immediately.
# Use after editing outbound_prompts/*.md on the VPS to skip the 30-second
# polling interval. The PromptLoader singleton is wired up in
# agents/registry.build_orchestrator(), so this works without any per-call
# state and is safe to invoke mid-traffic — agents pick up the new prompt
# on their next LLM turn.


@router.post("/admin/reload-prompts")
async def reload_prompts():
    from core.prompt_loader import get_loader

    loader = get_loader()
    if loader is None:
        return {"ok": False, "error": "prompt_loader not initialised"}
    changed = await loader.reload_all(force=True)
    fingerprints = loader.fingerprints()
    return {"ok": True, "reloaded": changed, "fingerprints": fingerprints}


# ── Outbound: single originate ──────────────────────────────────────────────


def _build_lead_context(lead: Lead, mobile: str, settings: Settings) -> dict:
    """Build the per-call context dict that gets seeded into the agent session."""
    raw_lang = (lead.preferred_language or "").strip().lower()
    lang = {"hinglish": "hi", "english": "en", "hindi": "hi",
            "tamil": "ta", "telugu": "te", "marathi": "mr"}.get(raw_lang, raw_lang)

    fmt = defaultdict(str, {
        "customer_name": lead.customer_name or "Sir/Madam",
        "phone_number":  mobile,
        "customer_type": lead.customer_type,
        "product_category": lead.product_category,
        "product_name":  lead.product_name or lead.product_category or "your vehicle",
        "purchase_date": lead.purchase_date,
        "address":       lead.address,
        "pincode":       lead.pincode,
        "preferred_language": lead.preferred_language,
    })
    welcome = settings.outbound_welcome_template.format_map(fmt)

    return {
        "customer_name":    lead.customer_name,
        "name":             lead.customer_name,       # legacy session key
        "phone_number":     mobile,
        "mobile":           mobile,                   # legacy session key
        "customer_type":    lead.customer_type,
        "product_category": lead.product_category,
        "product_name":     lead.product_name,
        "purchase_date":    lead.purchase_date,
        "address":          lead.address,
        "pincode":          lead.pincode,
        "preferred_language": lead.preferred_language,
        "language":         lang,
        "welcome_message":  welcome,
        "did":              settings.sip_trunk,
        "direction":        "outbound",
        "campaign_name":    lead.campaign_name,
    }


async def originate_lead(lead: Lead) -> dict:
    """
    Shared dial path used by both the single-originate endpoint and the
    campaign worker. Generates UUID, stashes lead context, invokes AMI.

    Uses TrunkPool when configured (via TRUNKS env) so a failing carrier or a
    flagged caller-ID rotates out automatically. Falls back to the single
    SIP_TRUNK / OUTBOUND_CALLER_ID values when no pool is available.
    """
    settings = Settings.from_env()
    if not settings.outbound_enabled:
        raise RuntimeError("outbound calling is disabled (set OUTBOUND_ENABLED=true)")

    tenant_id = settings.outbound_caller_id or "default"
    if not TenantRateLimiter.try_acquire(tenant_id):
        raise RuntimeError(f"rate limited (tenant={tenant_id})")

    pool = get_trunk_pool()
    if pool is None:
        # Bootstrap-time fallback (tests, scripts that import admin without main.py)
        if not settings.sip_trunk:
            raise RuntimeError("SIP_TRUNK is not configured (and TRUNKS pool not initialised)")
        pool = TrunkPool.from_settings(settings)

    mobile = re.sub(r"\D", "", lead.phone_number)
    if not re.fullmatch(r"\d{10,15}", mobile):
        raise ValueError(f"invalid phone_number: {lead.phone_number!r}")

    uuid_str = str(_uuid.uuid4())
    await outbound_call_store.put(uuid_str, _build_lead_context(lead, mobile, settings))

    binding = pool.acquire()
    channel = binding.channel(mobile)
    action_id = f"outbound-{uuid_str}"
    ami = await get_ami(settings)
    logger.info(
        f"[originate] uuid={uuid_str} channel={channel} "
        f"caller_id={binding.caller_id} action_id={action_id}"
    )
    try:
        resp = await ami.originate(
            channel=channel,
            context="outbound-ai-jcb",
            extension="s",
            priority=1,
            variables={
                "AUDIOSOCKET_UUID": uuid_str,
                "AUDIOSOCKET_DID":  mobile,
                "CUSTOMER_NAME":    lead.customer_name,
            },
            caller_id=binding.caller_id or binding.trunk_name,
            timeout_ms=30000,
            action_id=action_id,
        )
    except Exception as e:
        logger.error(f"[originate] AMI originate raised for uuid={uuid_str}: {e!r}")
        pool.report_failure(binding, e)
        await outbound_call_store.pop(uuid_str)
        raise

    logger.info(f"[originate] AMI response uuid={uuid_str} resp={resp}")
    if resp.get("Response") != "Success":
        pool.report_failure(binding, resp.get("Message") or resp)
        await outbound_call_store.pop(uuid_str)
        raise RuntimeError(f"AMI originate rejected: {resp.get('Message') or resp}")

    pool.report_success(binding)
    return {
        "uuid": uuid_str,
        "action_id": action_id,
        "status": "originated",
        "trunk": binding.trunk_name,
        "caller_id": binding.caller_id,
    }


@router.post("/api/outbound/originate")
async def outbound_originate(req: OriginateRequest):
    lead = Lead(
        customer_name=req.customer_name,
        phone_number=req.phone_number,
        customer_type=req.customer_type,
        product_category=req.product_category,
        product_name=req.product_name,
        purchase_date=req.purchase_date,
        address=req.address,
        pincode=req.pincode,
        preferred_language=req.preferred_language,
    )
    try:
        return await originate_lead(lead)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Outbound: batch campaign ────────────────────────────────────────────────


@router.post("/api/outbound/campaign/upload")
async def campaign_upload(
    file: UploadFile = File(...),
    rate_per_min: int = Form(6),
    campaign_name: str = Form(""),
    campaign_type: str = Form(""),
):
    manager = get_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="campaign manager not initialized (is OUTBOUND_ENABLED=true?)",
        )

    suffix = Path(file.filename or "").suffix.lower() or ".csv"
    if suffix not in (".csv", ".xlsx", ".xlsm"):
        raise HTTPException(status_code=400, detail=f"unsupported file type: {suffix}")

    # Persist to a temp file for the parser (openpyxl needs a real path for read_only mode)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        leads = parse_file(tmp_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"parse failed: {e}")
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass

    if not leads:
        raise HTTPException(status_code=400, detail="no leads parsed from file")

    # Single source of truth for pacing: 6 dials/min per lead in the batch.
    # 1 lead → 6/min, 2 → 12/min, 10 → 60/min, etc. The form's rate_per_min
    # input is shown disabled in the UI and is intentionally ignored here.
    #
    # Ceiling: never exceed what the per-tenant rate limiter can actually
    # sustain (RATE_LIMIT_RATE_PER_SEC × 60). Going past this just causes
    # 'rate limited (tenant=…)' rejections in originate_lead() and burns
    # SIP slots for nothing. Floor of 6 keeps batches with 0 valid leads
    # from creating a 0-rate campaign.
    settings = Settings.from_env()
    rate_cap = max(6, int(settings.rate_limit_rate_per_sec * 60))
    rate_per_min = max(1, min(len(leads) * 6, rate_cap))

    campaign = manager.create(leads, rate_per_min=rate_per_min, name=campaign_name, campaign_type=campaign_type)
    return {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "campaign_type": campaign.campaign_type,
        "lead_count": len(leads),
        "rate_per_min": campaign.rate_per_min,
    }


@router.get("/api/outbound/campaigns")
async def list_campaigns():
    manager = get_manager()
    if manager is None:
        return {"campaigns": []}
    return {"campaigns": manager.list_campaigns()}


@router.get("/api/outbound/campaign/{campaign_id}")
async def campaign_status(campaign_id: str):
    manager = get_manager()
    if manager is None:
        raise HTTPException(status_code=404, detail="campaign manager not initialized")
    snap = manager.snapshot(campaign_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return snap


@router.post("/api/outbound/campaign/{campaign_id}/stop")
async def campaign_stop(campaign_id: str):
    manager = get_manager()
    if manager is None:
        raise HTTPException(status_code=404, detail="campaign manager not initialized")
    if not manager.stop(campaign_id):
        raise HTTPException(status_code=400, detail="campaign not running")
    return {"status": "stopped"}


# ── Outbound: per-call detail (transcript + extracted data) ────────────────


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_latest_json(directory: Path, prefix: str) -> dict | None:
    """Read the newest JSON file in `directory` whose name starts with `prefix_`."""
    if not directory.exists():
        return None
    matches = sorted(directory.glob(f"{prefix}_*.json"), reverse=True)
    if not matches:
        return None
    try:
        with open(matches[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Failed to read {matches[0]}")
        return None


def _find_by_mobile_or_time(
    directory: Path,
    mobile: str,
    started_at: float,
    max_diff_sec: float = 1800.0,
) -> tuple[dict | None, str | None]:
    """
    Fallback lookup when the AudioSocket UUID doesn't match the lead.uuid
    (e.g. the dialplan didn't propagate AUDIOSOCKET_UUID, so the call_sid
    on disk differs from the lead.uuid in the campaign).

    Strategy: scan all JSON files in `directory` and pick the one whose payload
    has a matching mobile AND whose mtime is within `max_diff_sec` of started_at.
    Returns (payload, matched_uuid_prefix) or (None, None).
    """
    if not directory.exists():
        return None, None
    best_payload = None
    best_uuid = None
    best_diff = None
    mobile_norm = re.sub(r"\D", "", str(mobile or ""))[-10:]
    for f in directory.glob("*.json"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        diff = abs(mtime - started_at) if started_at else max_diff_sec
        if started_at and diff > max_diff_sec:
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        # Prefer a mobile match, fall back to time match alone
        payload_mobile = (
            payload.get("mobile")
            or (payload.get("metadata") or {}).get("mobile")
            or ""
        )
        payload_mobile_norm = re.sub(r"\D", "", str(payload_mobile))[-10:]
        mobile_matches = bool(mobile_norm) and payload_mobile_norm == mobile_norm
        if mobile_matches and (best_diff is None or diff < best_diff):
            best_payload = payload
            best_diff = diff
            # Filename is "{uuid}_{ts}.json" — pull the uuid prefix
            best_uuid = f.stem.rsplit("_", 2)[0] if "_" in f.stem else f.stem
            if diff < 5:
                break  # exact-enough match; stop scanning
    return best_payload, best_uuid


@router.get("/api/outbound/call/{call_uuid}")
async def call_detail(call_uuid: str):
    """
    Return combined lead snapshot + transcript + extracted data for one call.

    Primary lookup: lead.uuid == file prefix (the Asterisk call_sid).
    Fallback: when the dialplan didn't propagate AUDIOSOCKET_UUID, the on-disk
    files carry the AudioSocket-generated UUID instead, so we re-match by the
    lead's mobile + started_at timestamp.
    """
    transcripts_dir = _PROJECT_ROOT / "transcripts"
    extracted_dir = _PROJECT_ROOT / "extracted_data"

    # Locate the lead inside any campaign (so we can show campaign context)
    manager = get_manager()
    lead_dict: dict | None = None
    campaign_id: str | None = None
    if manager is not None:
        for camp in manager.list_campaigns():
            for lead in camp.get("leads", []):
                if lead.get("uuid") == call_uuid:
                    lead_dict = lead
                    campaign_id = camp.get("id")
                    break
            if lead_dict:
                break

    transcript_data = await asyncio.to_thread(_read_latest_json, transcripts_dir, call_uuid)
    extracted = await asyncio.to_thread(_read_latest_json, extracted_dir, call_uuid)

    # Fallback: AudioSocket UUID mismatch — scan by mobile + time
    matched_uuid: str | None = None
    if lead_dict and (transcript_data is None or extracted is None):
        mobile = lead_dict.get("phone_number") or lead_dict.get("mobile") or ""
        started_at = float(lead_dict.get("started_at") or 0)
        if transcript_data is None:
            transcript_data, matched_uuid = await asyncio.to_thread(
                _find_by_mobile_or_time, transcripts_dir, mobile, started_at
            )
        if extracted is None:
            extracted, _ = await asyncio.to_thread(
                _find_by_mobile_or_time, extracted_dir, mobile, started_at
            )

    if lead_dict is None and transcript_data is None and extracted is None:
        raise HTTPException(status_code=404, detail="call not found")

    duration = 0.0
    if transcript_data and "duration_seconds" in transcript_data:
        duration = float(transcript_data.get("duration_seconds") or 0)
    elif lead_dict:
        try:
            duration = max(0.0, float(lead_dict.get("finished_at") or 0) - float(lead_dict.get("started_at") or 0))
        except Exception:
            duration = 0.0

    # Look up an associated recording URL via the extracted_data we found
    # (the recorder publishes a Cloudinary URL keyed on the actual call_sid).
    recording_url = ""
    if extracted and isinstance(extracted, dict):
        recording_url = extracted.get("recording_url") or ""
    if not recording_url and matched_uuid:
        # Cloudinary public URL pattern from call_recorder.upload_to_cloudinary
        # We don't know the exact upload timestamp suffix without an index, so
        # surface the matched on-disk uuid for client-side debugging.
        recording_url = ""

    return {
        "uuid": call_uuid,
        "matched_uuid": matched_uuid,
        "campaign_id": campaign_id,
        "lead": lead_dict,
        "duration_seconds": duration,
        "transcript": (transcript_data or {}).get("transcript") or [],
        "transcript_meta": {
            "timestamp": (transcript_data or {}).get("timestamp"),
            "language": (transcript_data or {}).get("language"),
        } if transcript_data else None,
        "extracted": extracted,
        "recording_url": recording_url,
    }


# ── Outbound: transcript translation ───────────────────────────────────────


@router.post("/api/outbound/translate")
async def translate_transcript(req: TranslateRequest):
    """Translate a call transcript to English using the configured LLM."""
    if not req.transcript:
        return {"translated": []}

    settings = Settings.from_env()
    from providers.llm.groq_provider import GroqLLM

    llm = GroqLLM(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        temperature=0.1,
        max_tokens=4096,
        timeout=60,
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a translation assistant. Translate the given conversation transcript to English. "
                "Return ONLY a valid JSON array with no extra text, markdown, or code fences. "
                "Each element must have exactly two keys: \"role\" (unchanged) and \"content\" (translated to English). "
                "If a message is already in English, keep the content unchanged."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Source language: {req.language}\n\n"
                f"Transcript:\n{json.dumps(req.transcript, ensure_ascii=False)}"
            ),
        },
    ]

    try:
        raw = await llm.chat(messages)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}") from e

    # Strip markdown code fences if the model wrapped the JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[1:])
        cleaned = cleaned.rstrip("`").strip()

    try:
        translated = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"LLM returned invalid JSON: {e}") from e

    if not isinstance(translated, list):
        raise HTTPException(status_code=502, detail="LLM returned unexpected structure")

    return {"translated": translated}


# ── Outbound: HTML dashboard ────────────────────────────────────────────────


@router.get("/admin/outbound")
async def outbound_dashboard():
    html_path = Path(__file__).parent / "outbound_dashboard.html"
    if not html_path.exists():
        return JSONResponse({"error": "dashboard html not found"}, status_code=500)
    return FileResponse(str(html_path), media_type="text/html")
