"""
Sprint 7: Mock CRM — returns synthetic customer data for testing.
Replace with real CRM adapter (Salesforce, Zoho, SAP, etc.) in production.

Set CRM_PROVIDER=mock (default) or CRM_PROVIDER=http to switch.
For HTTP: set CRM_BASE_URL and CRM_API_KEY env vars.
"""
from __future__ import annotations

import logging
import os

from providers.crm.base import BaseCRMProvider, CustomerRecord

logger = logging.getLogger(__name__)


class MockCRMProvider(BaseCRMProvider):
    """Returns pre-seeded mock data — useful for demos and integration tests."""

    _MOCK_DB: dict[str, dict] = {
        "9876543210": {
            "name": "Rahul Sharma",
            "account_tier": "premium",
            "products": ["Godrej AC GIC 18ETC5-WTA", "Godrej Refrigerator"],
            "open_complaints": 1,
            "last_complaint_desc": "AC not cooling",
            "preferred_language": "hi",
        },
        "9871234567": {
            "name": "Priya Mehta",
            "account_tier": "standard",
            "products": ["Godrej Washing Machine WF EON 7.0"],
            "open_complaints": 0,
            "last_complaint_desc": "",
            "preferred_language": "en",
        },
    }

    async def lookup(self, mobile: str) -> CustomerRecord:
        # Normalize — strip country code if present
        clean = mobile.lstrip("+").lstrip("91") if mobile.startswith(("+91", "91")) else mobile
        clean = clean.replace(" ", "").replace("-", "")

        data = self._MOCK_DB.get(clean) or self._MOCK_DB.get(mobile)
        if not data:
            logger.debug(f"[MockCRM] No record for {mobile}")
            return CustomerRecord(found=False, mobile=mobile)

        logger.info(f"[MockCRM] Found customer for {mobile}: {data['name']}")
        return CustomerRecord(
            found=True,
            name=data["name"],
            mobile=mobile,
            account_tier=data.get("account_tier", "standard"),
            products=data.get("products", []),
            open_complaints=data.get("open_complaints", 0),
            last_complaint_desc=data.get("last_complaint_desc", ""),
            preferred_language=data.get("preferred_language", ""),
        )


class HTTPCRMProvider(BaseCRMProvider):
    """
    Real CRM adapter via HTTP REST API.
    Configure: CRM_BASE_URL, CRM_API_KEY env vars.
    """

    def __init__(self):
        import httpx
        self._base_url = os.environ.get("CRM_BASE_URL", "").rstrip("/")
        self._api_key = os.environ.get("CRM_API_KEY", "")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=2.0,  # never block call start for >2s
        )

    async def lookup(self, mobile: str) -> CustomerRecord:
        if not self._base_url:
            return CustomerRecord(found=False)
        try:
            r = await self._client.get(f"/customers/lookup", params={"mobile": mobile})
            if r.status_code == 404:
                return CustomerRecord(found=False, mobile=mobile)
            r.raise_for_status()
            d = r.json()
            return CustomerRecord(
                found=True,
                name=d.get("name", ""),
                mobile=mobile,
                account_tier=d.get("tier", "standard"),
                products=d.get("products", []),
                open_complaints=d.get("open_complaints", 0),
                last_complaint_desc=d.get("last_complaint", ""),
                preferred_language=d.get("language", ""),
            )
        except Exception as e:
            logger.warning(f"[HTTPCRM] Lookup failed for {mobile}: {e}")
            return CustomerRecord(found=False, mobile=mobile)

    async def close(self) -> None:
        await self._client.aclose()


def create_crm_provider() -> BaseCRMProvider:
    provider = os.environ.get("CRM_PROVIDER", "mock").lower()
    if provider == "http":
        return HTTPCRMProvider()
    return MockCRMProvider()
