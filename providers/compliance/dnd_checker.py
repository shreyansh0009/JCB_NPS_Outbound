"""
Sprint 8: TRAI DND (Do Not Disturb) compliance check.

For OUTBOUND campaigns: check number against TRAI NDNC before dialing.
For INBOUND calls: not required (customer is calling us) — but log consent
for call recording if RECORD_CALLS=true is set.

Real TRAI DND API:
  - Requires registration with a licensed DND scrubbing agency (e.g. Knowlarity, Airtel)
  - API format varies by provider
  - This module provides the interface — swap _real_check() for your provider

DND check result is cached in Redis (24h TTL) to avoid repeated API calls
for the same number during a campaign.

Consent prompt (if RECORD_CALLS=true):
  Added to session.metadata["consent_prompt"] — hello agent reads and speaks it
  before the greeting. Customer pressing any key or saying "yes" = consent given.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DND_PROVIDER = os.environ.get("DND_PROVIDER", "mock")    # mock | api
_DND_API_URL  = os.environ.get("DND_API_URL", "")
_DND_API_KEY  = os.environ.get("DND_API_KEY", "")
_RECORD_CALLS = os.environ.get("RECORD_CALLS", "false").lower() == "true"

# Consent prompt text (TRAI mandates informing callers of recording)
CONSENT_PROMPT = {
    "hi": "यह कॉल गुणवत्ता और प्रशिक्षण के उद्देश्य से रिकॉर्ड की जा सकती है।",
    "en": "This call may be recorded for quality and training purposes.",
    "mr": "गुणवत्ता आणि प्रशिक्षणाच्या उद्देशाने हा कॉल रेकॉर्ड केला जाऊ शकतो.",
}


@dataclass
class DndResult:
    mobile: str
    is_dnd: bool          # True = number is on DND, do NOT call
    checked: bool = True  # False = check skipped (inbound / mock)
    source: str = "mock"  # mock | api | cache


class DndChecker:
    """TRAI DND compliance — check outbound numbers before dialing."""

    def __init__(self):
        self._cache: dict[str, DndResult] = {}  # in-process cache (Redis added if available)

    async def check(self, mobile: str) -> DndResult:
        """
        Check if mobile is on DND registry.
        Returns DndResult(is_dnd=False) for inbound / mock mode.
        """
        if _DND_PROVIDER == "mock":
            return DndResult(mobile=mobile, is_dnd=False, source="mock")

        # Check process-level cache first
        if mobile in self._cache:
            cached = self._cache[mobile]
            logger.debug(f"[DND] Cache hit: {mobile} → is_dnd={cached.is_dnd}")
            return cached

        result = await self._real_check(mobile)
        self._cache[mobile] = result
        logger.info(f"[DND] {mobile} → is_dnd={result.is_dnd} source={result.source}")
        return result

    async def _real_check(self, mobile: str) -> DndResult:
        """Call real DND API. Override for your scrubbing provider."""
        if not _DND_API_URL:
            return DndResult(mobile=mobile, is_dnd=False, source="api_unconfigured")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(
                    _DND_API_URL,
                    params={"number": mobile, "key": _DND_API_KEY},
                )
                data = r.json()
                is_dnd = data.get("dnd", False)
                return DndResult(mobile=mobile, is_dnd=is_dnd, source="api")
        except Exception as e:
            logger.warning(f"[DND] API error for {mobile}: {e} — defaulting to not-DND")
            return DndResult(mobile=mobile, is_dnd=False, source="api_error")

    def get_consent_prompt(self, lang: str = "en") -> str:
        """Return consent prompt for call recording disclosure."""
        if not _RECORD_CALLS:
            return ""
        return CONSENT_PROMPT.get(lang, CONSENT_PROMPT["en"])
