"""
Email notification — sends service confirmation email via Resend after case creation.

Mirrors the Node backend's sendMail utility using the Resend HTTP API.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _build_email_html(
    user_name: str,
    case_id: str,
    issue_desc: str,
    address: str,
    mobile: str,
    email: str,
) -> str:
    """Build the service confirmation HTML email body."""
    service_time = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")

    return f"""\
<h2 style="color: #004d40;">Godrej Appliances \u2013 Service Update</h2>

<p>Dear {user_name},</p>

<p>We have received your request regarding <b>{issue_desc}</b>.</p>

<p>
  <b>Case ID:</b> {case_id}<br/>
</p>

<p>
  <b>Registered Address:</b><br/>
  {address or "Address to be updated"}<br/>
  <b>Service Time:</b> {service_time}
</p>

<p>
  <b>Registered Phone:</b> {mobile}<br/>
  <b>Registered Email:</b> {email}
</p>

<p style="margin-top: 30px;">Regards,<br/><b>Godrej Appliances Support Team</b></p>
"""


async def send_email(
    settings,
    session,
    case_number: str,
) -> Optional[dict]:
    """
    Send service confirmation email via Resend API.

    Returns the Resend response dict on success, None on failure.
    Never raises — logs errors instead.
    """
    if not settings.resend_api_key:
        logger.debug("Resend API key not configured — skipping email")
        return None

    try:
        name = session.get("name", "Guest")
        mobile = session.get("mobile", "")
        intent = session.get("intent", "Service Request")
        address = session.get("address", "")
        email_to = session.get("email", "")

        if not email_to or "@" not in email_to:
            logger.info("No valid customer email — skipping email notification")
            return None

        case_id = f"SR-{case_number}" if case_number else "SR-PENDING"
        html = _build_email_html(name, case_id, intent, address, mobile, email_to)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json={
                    "from": settings.resend_from_email,
                    "to": email_to,
                    "subject": f"Godrej Appliances \u2013 Service Update \u2014 Case {case_id}",
                    "html": html,
                },
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            result = resp.json()

        logger.info(f"Email sent to {email_to} for case {case_id}")
        return result

    except Exception:
        logger.exception("Failed to send email notification")
        return None
