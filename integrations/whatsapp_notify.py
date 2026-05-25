"""
WhatsApp notification — sends service confirmation via WhatsApp Business API.

Uses the Facebook Graph API with a pre-approved WhatsApp template message,
mirroring the Node backend's implementation.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def send_whatsapp(
    settings,
    session,
    case_number: str,
) -> Optional[dict]:
    """
    Send WhatsApp template message to the customer via Facebook Graph API.

    Returns the WhatsApp API response dict on success, None on failure.
    Never raises — logs errors instead.
    """
    if not settings.whatsapp_access_token:
        logger.debug("WhatsApp access token not configured — skipping")
        return None

    try:
        name = session.get("name", "Guest")
        mobile = session.get("mobile", "")
        intent = session.get("intent", "Service Request")
        address = session.get("address", "Address to be updated")
        product = session.get("product", "")

        if not mobile:
            logger.info("No customer mobile number — skipping WhatsApp notification")
            return None

        # Strip country code prefix, then add 91
        clean_mobile = re.sub(r"^(\+91|91)", "", mobile)
        wa_to = f"91{clean_mobile}"

        now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

        # Template parameters — order must match the approved WhatsApp template
        parameters = [
            name,
            now_str,
            address,
            product,
            intent,
        ]

        payload = {
            "messaging_product": "whatsapp",
            "to": wa_to,
            "type": "template",
            "template": {
                "name": settings.whatsapp_template_name,
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": p} for p in parameters
                        ],
                    },
                ],
            },
        }

        url = f"https://graph.facebook.com/v22.0/{settings.whatsapp_phone_number_id}/messages"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.whatsapp_access_token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            result = resp.json()

        logger.info(f"WhatsApp message sent to {wa_to} for case SR-{case_number}")
        return result

    except Exception:
        logger.exception("Failed to send WhatsApp notification")
        return None
