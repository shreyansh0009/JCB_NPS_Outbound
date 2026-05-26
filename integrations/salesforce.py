"""
Salesforce integration — creates an NPS Survey record via Apex REST after each call.

Auth: OAuth2 password grant.  The access token is cached in memory
and auto-refreshed on 401 or expiry.

Usage (called from transcript_logger):
    from integrations.salesforce import create_nps_survey
    result = await create_nps_survey(settings, session, transcript_text, duration_seconds)
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Module-level token cache ────────────────────────────────────────────────
_access_token: str = ""
_token_expires_at: float = 0.0  # monotonic timestamp


# ── Q1-Q4 keyword signatures for extracting structured question ratings ──────

_Q_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Machine Quality", (
        "machine quality", "machine की quality", "machine ki quality",
        "equipment quality", "machine मिली", "overall quality",
        "on the machine quality",
    )),
    ("Order Accuracy", (
        "model और attachments", "order किए थे", "correct model",
        "order accuracy", "model and attachments", "as per your order",
    )),
    ("Delivery", (
        "delivery की बात", "on delivery", "deliver हुई",
        "delivered on time", "delivery condition", "machine deliver",
    )),
    ("Documentation & Communication", (
        "documents के बारे", "on documentation", "delivery challan",
        "invoice", "warranty card", "documentation",
    )),
]


def _format_duration(seconds: float) -> str:
    total = int(math.ceil(seconds))
    mins = total // 60
    secs = total % 60
    if mins and secs:
        return f"{mins} min {secs} sec"
    if mins:
        return f"{mins} min"
    return f"{secs} sec"


def _extract_question_ratings(history: list[dict]) -> list[dict]:
    """
    Scan conversation history for Q1-Q4 assistant questions and capture
    the next user response as the rating string.
    """
    ratings: list[dict] = []
    for label, keywords in _Q_KEYWORDS:
        for i, msg in enumerate(history):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "").lower()
            if not any(kw.lower() in content for kw in keywords):
                continue
            # Capture the next user turn as the answer
            for j in range(i + 1, len(history)):
                if history[j].get("role") == "user":
                    answer = (history[j].get("content", "") or "").strip()
                    if answer:
                        ratings.append({"question": label, "rating": answer[:500]})
                    break
            break  # first match per question is enough
    return ratings


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


# ── NPS Survey creation ────────────────────────────────────────────────────

async def create_nps_survey(
    settings,
    session,
    transcript_text: str,
    duration_seconds: float,
) -> Optional[dict]:
    """
    Create a Salesforce NPS_Survey__c record via Apex REST endpoint.

    Returns the SF response dict on success, None on failure.
    Never raises — logs errors instead.
    """
    if not settings.sf_instance_url:
        logger.debug("Salesforce not configured — skipping NPS survey creation")
        return None

    try:
        name = session.get("name", "") or session.get("customer_name", "") or "Guest"
        mobile = session.get("mobile", "") or session.get("customer_mobile", "")
        nps_rating = session.get("nps_rating")
        remark = session.get("intent", "") or ""

        # Build Q1-Q4 question ratings from conversation history
        question_ratings = (
            session.get("question_ratings")
            or _extract_question_ratings(session.history)
        )

        payload = {
            "customerName": name,
            "mobileNumber": mobile,
            "overallRating": float(nps_rating) if nps_rating is not None else None,
            "remark": remark,
            "recordingLink": session.get("recording_url", ""),
            "questionRatings": question_ratings,
            "callDuration": _format_duration(duration_seconds),
            "campaignName": session.get("campaign_name", ""),
        }

        endpoint = f"{settings.sf_instance_url}/services/apexrest/NPSSurveyAPI/"
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

            resp.raise_for_status()
            result = resp.json()

        survey_id = result.get("surveyId", "UNKNOWN")
        logger.info(f"Salesforce NPS survey created: {survey_id} (rating={nps_rating})")
        return result

    except Exception:
        logger.exception("Failed to create Salesforce NPS survey")
        return None


# Backward-compat alias used by transcript_logger
create_case = create_nps_survey
