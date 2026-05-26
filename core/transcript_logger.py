"""
Transcript logger — saves call transcript + extracted data after each call.

Writes two files per call:
  transcripts/{call_sid}_{timestamp}.json      — full conversation history
  extracted_data/{call_sid}_{timestamp}.json   — clean extracted fields only

Uses asyncio.to_thread for non-blocking file I/O (same pattern as latency_logger.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from core.session import CallSession

logger = logging.getLogger(__name__)

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_TRANSCRIPTS_DIR = _PROJECT_ROOT / "transcripts"
_EXTRACTED_DIR   = _PROJECT_ROOT / "extracted_data"

# Metadata keys to include in both output files
_META_KEYS = ("name", "mobile", "intent", "address", "product", "language")

# 6-digit Indian pincode
_PINCODE_RE = re.compile(r'\b([1-9][0-9]{5})\b')


def _extract_pincode(address: str) -> str:
    """Pull the first 6-digit pincode out of an address string."""
    m = _PINCODE_RE.search(address)
    return m.group(1) if m else ""


async def save_transcript(
    call_sid: str,
    session: CallSession,
    call_start_mono: float,
) -> None:
    """
    Write full transcript + extracted data files for a completed call.

    Parameters
    ----------
    call_sid : str
        Asterisk UNIQUEID for the call.
    session : CallSession
        The session object containing history and metadata.
    call_start_mono : float
        ``time.monotonic()`` captured at call start, used to compute duration.
    """
    try:
        now      = datetime.now()
        duration = time.monotonic() - call_start_mono
        ts_str   = now.strftime("%Y%m%d_%H%M%S")
        filename = f"{call_sid}_{ts_str}.json"

        meta = {k: session.get(k, "") for k in _META_KEYS}

        # ── Full transcript ───────────────────────────────────────────────
        transcript_record = {
            "call_sid":        call_sid,
            "session_id":      session.session_id,
            "timestamp":       now.isoformat(timespec="seconds"),
            "duration_seconds": round(duration, 1),
            "language":        session.current_language,
            "metadata":        meta,
            "transcript":      session.history,
        }
        await asyncio.to_thread(
            _write_sync, _TRANSCRIPTS_DIR / filename, transcript_record
        )
        logger.info(f"[{call_sid}] Transcript saved → transcripts/{filename}")

        # ── Extracted data ────────────────────────────────────────────────
        address = meta.get("address", "")
        extracted_record = {
            "call_sid":        call_sid,
            "session_id":      session.session_id,
            "timestamp":       now.isoformat(timespec="seconds"),
            "duration_seconds": round(duration, 1),
            "language":        session.current_language,
            "name":            meta.get("name", ""),
            "mobile":          meta.get("mobile", ""),
            "intent":          meta.get("intent", ""),
            "product":         meta.get("product", ""),
            "address":         address,
            "pincode":         _extract_pincode(address),
            "recording_url":   session.get("recording_url", ""),
        }
        await asyncio.to_thread(
            _write_sync, _EXTRACTED_DIR / filename, extracted_record
        )
        logger.info(f"[{call_sid}] Extracted data saved → extracted_data/{filename}")

        # ── Post-call integrations ───────────────────────────────────────
        from config.settings import Settings
        from integrations.salesforce import create_nps_survey
        from integrations.email_notify import send_email
        from integrations.whatsapp_notify import send_whatsapp

        settings = Settings.from_env()
        transcript_text = "\n".join(
            f"{'Agent' if m.get('role') == 'assistant' else 'Customer'}: {m.get('content', '')}"
            for m in session.history
        )

        # 1. Create Salesforce NPS survey record
        sf_result = await create_nps_survey(settings, session, transcript_text, duration)
        case_number = sf_result.get("surveyId", "") if sf_result else ""

        # 2. Send email + WhatsApp notifications (need case number from SF)
        await send_email(settings, session, case_number)
        await send_whatsapp(settings, session, case_number)

        # 3. Mark the outbound-campaign lead as completed so the dashboard
        #    (and the call-detail page) can stop showing "originated" forever.
        #    Pass mobile as a fallback key — when the Asterisk dialplan doesn't
        #    propagate AUDIOSOCKET_UUID, call_sid won't match lead.uuid and
        #    mark_completed re-links the lead by mobile.
        try:
            from core.outbound_campaign import get_manager
            manager = get_manager()
            if manager is not None:
                await manager.mark_completed(
                    call_sid,
                    success=True,
                    mobile=meta.get("mobile", ""),
                )
        except Exception:
            logger.exception(f"[{call_sid}] Failed to update campaign lead status")

    except Exception:
        logger.exception(f"[{call_sid}] Failed to save transcript/extracted data")


def _write_sync(filepath: Path, record: dict) -> None:
    """Synchronous JSON write — runs in thread pool."""
    os.makedirs(filepath.parent, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
