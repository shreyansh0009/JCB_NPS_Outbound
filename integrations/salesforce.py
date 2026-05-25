"""
Salesforce integration — creates a Case via Apex REST after each call.

Auth: OAuth2 refresh-token flow.  The access token is cached in memory
and auto-refreshed on 401 or expiry.

Usage (called from transcript_logger):
    from integrations.salesforce import create_case
    result = await create_case(settings, session, transcript_text, duration_seconds)
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Module-level token cache ────────────────────────────────────────────────
_access_token: str = ""
_token_expires_at: float = 0.0  # monotonic timestamp


# ── Case type classification (mirrors Node backend) ─────────────────────────

_COMPLAINT_KEYWORDS = [
    "complaint", "rude", "delay", "wrong", "poor",
    "service complaint", "technician complaint",
    "shikayat", "kharab service",
]

_SERVICE_KEYWORDS = [
    "not working", "leak", "water leaking", "kharab", "repair",
    "ac not working", "washing machine not working",
    "issue", "problem", "install", "installation",
    "service", "cooling", "heating", "noise",
]


def _classify_case_type(issue_desc: str) -> str:
    """Classify issue description into Salesforce Case Subject."""
    if not issue_desc:
        return "Service Appointment"
    lower = issue_desc.lower()
    if any(kw in lower for kw in _COMPLAINT_KEYWORDS):
        return "Complaint"
    if any(kw in lower for kw in _SERVICE_KEYWORDS):
        return "Service Appointment"
    return "Service Appointment"


def _format_duration(seconds: float) -> str:
    """Format duration as human-readable string (matches Node backend)."""
    total_ms = int(seconds * 1000)
    minutes = total_ms // 60_000
    remaining = total_ms % 60_000
    secs = remaining // 1000
    ms = remaining % 1000
    parts = []
    if minutes > 0:
        parts.append(f"{minutes} min")
    if secs > 0:
        parts.append(f"{secs} sec")
    if ms > 0:
        parts.append(f"{ms} ms")
    return " ".join(parts) or "0 sec"


_PINCODE_RE = re.compile(r"\b([1-9]\d{5})\b")


def _extract_pincode(address: str) -> str:
    m = _PINCODE_RE.search(address or "")
    return m.group(1) if m else ""


# ── OAuth2 token refresh ───────────────────────────────────────────────────

async def _fetch_access_token(settings) -> str:
    """
    Obtain access_token via Salesforce OAuth2 password grant.
    Caches the result in module-level globals.
    """
    global _access_token, _token_expires_at

    url = "https://login.salesforce.com/services/oauth2/token"
    payload = {
        "grant_type": "password",
        "client_id": settings.sf_client_id,
        "client_secret": settings.sf_client_secret,
        "username": settings.sf_username,
        "password": settings.sf_password,
    }

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(url, data=payload)
        resp.raise_for_status()
        data = resp.json()

    _access_token = data["access_token"]
    # Salesforce tokens typically last ~2 hours; refresh proactively at 1h50m
    _token_expires_at = time.monotonic() + 6600
    logger.info("Salesforce access token obtained via password grant")
    return _access_token


async def _get_access_token(settings) -> str:
    """Return a valid access token, fetching a new one if needed."""
    global _access_token, _token_expires_at

    # Use pre-configured token if set and no username/password available
    if not settings.sf_username and settings.sf_access_token:
        return settings.sf_access_token

    # Fetch if expired or empty
    if not _access_token or time.monotonic() >= _token_expires_at:
        return await _fetch_access_token(settings)

    return _access_token


# ── Case creation ──────────────────────────────────────────────────────────

async def create_case(
    settings,
    session,
    transcript_text: str,
    duration_seconds: float,
) -> Optional[dict]:
    """
    Create a Salesforce Case via Apex REST endpoint.

    Returns the SF response dict on success, None on failure.
    Never raises — logs errors instead.
    """
    if not settings.sf_instance_url:
        logger.debug("Salesforce not configured — skipping case creation")
        return None

    try:
        name    = session.get("name") or session.get("customer_name") or "Guest"
        mobile  = session.get("mobile") or session.get("phone_number") or ""
        intent  = session.get("intent", "")
        address = session.get("address", "")
        # Use pincode from session directly first; fall back to regex extraction
        pincode = (
            session.get("pincode")
            or session.get("parsed_pincode")
            or _extract_pincode(address)
            or ""
        )

        # Build a human-readable issue description for NPS calls
        nps_rating     = session.get("nps_rating")
        product_name   = session.get("product_name") or session.get("product_category") or ""
        customer_type  = session.get("customer_type") or ""
        purchase_date  = session.get("purchase_date") or ""
        reported_issue = session.get("reported_issue") or intent or ""

        if nps_rating is not None:
            issue_parts = [f"NPS Rating: {nps_rating}/10"]
            if product_name:
                issue_parts.append(f"Product: {product_name}")
            if customer_type:
                issue_parts.append(f"Customer Type: {customer_type}")
            if purchase_date:
                issue_parts.append(f"Purchase Date: {purchase_date}")
            if reported_issue:
                issue_parts.append(f"Feedback: {reported_issue}")
            issue_desc = " | ".join(issue_parts)
            case_type  = "NPS Feedback" if nps_rating >= 8 else "NPS Feedback - Low Score"
        else:
            issue_desc = reported_issue or "NPS Feedback Call"
            case_type  = _classify_case_type(reported_issue)

        duration_str = _format_duration(duration_seconds)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        duration_minutes = max(1, round(duration_seconds / 60))

        payload = {
            "Subject": case_type,
            "operation": "insert",
            "user_name": name,
            "Mobile": mobile,
            "Pincode": pincode,
            "issuedesc": issue_desc,
            "fulladdress": address,
            "email": " ",
            "preferred_date": now_str,
            "recording_link": session.get("recording_url", ""),
            "transcript": transcript_text,
            "DurationInMinutes": duration_minutes,
            "conversationDueration": duration_str,
            "sentiment": "Neutral",
            "Origin": "Phone",
            "Priority": "Medium" if (nps_rating or 0) >= 8 else "High",
        }

        logger.info(
            f"Salesforce case payload: name={name!r} mobile={mobile!r} "
            f"pincode={pincode!r} nps={nps_rating} case_type={case_type!r}"
        )

        endpoint = f"{settings.sf_instance_url}/services/apexrest/caseService"
        token = await _get_access_token(settings)

        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            # Auto-refresh on 401 and retry once
            if resp.status_code == 401 and settings.sf_username:
                logger.warning("Salesforce 401 — refreshing token and retrying")
                token = await _fetch_access_token(settings)
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )

            if resp.status_code != 200:
                logger.error(
                    f"Salesforce HTTP {resp.status_code} — body: {resp.text[:500]}"
                )
            resp.raise_for_status()
            result = resp.json()

        case_number = result.get("caseNumber", "UNKNOWN")
        if case_number is None:
            logger.warning(
                f"Salesforce returned caseNumber=null — full response: {result}"
            )
        else:
            logger.info(f"Salesforce case created: {case_number} ({case_type})")
        return result

    except Exception:
        logger.exception("Failed to create Salesforce case")
        return None
