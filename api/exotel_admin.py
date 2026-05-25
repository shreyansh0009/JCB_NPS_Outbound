"""
Exotel admin endpoints — make outbound calls via Exotel REST API.

POST /exotel/call/outbound  →  Agent calls the user
POST /exotel/status         →  Exotel call status callback (lifecycle logging)

Nothing in the existing admin.py or twilio.py is changed.
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exotel", tags=["exotel"])


class ExotelOutboundRequest(BaseModel):
    to: str     # Number to call — Indian mobile e.g. "09876543210" or "+919876543210"
    from_: str  # Your ExoPhone  e.g. "09513886363"


@router.post("/call/outbound")
async def make_exotel_outbound_call(req: ExotelOutboundRequest):
    """
    Trigger an outbound call from your ExoPhone to the user.

    How it works:
    1. We call Exotel REST API with To, From, and the App URL.
    2. Exotel calls the user's phone.
    3. When the user answers, Exotel runs your "AI Voice Agent" app (App ID 1208054).
    4. That app contains the Voicebot Applet which opens a WebSocket to
       wss://YOUR_SERVER/ws/exotel/{call_sid}
    5. StreamingPipeline takes over — STT → LLM → TTS — just like inbound.
    """
    api_key     = os.environ.get("EXOTEL_API_KEY", "")
    api_token   = os.environ.get("EXOTEL_API_TOKEN", "")
    account_sid = os.environ.get("EXOTEL_ACCOUNT_SID", "")   # e.g. "crmlanding5"
    subdomain   = os.environ.get("EXOTEL_SUBDOMAIN", "@api.in.exotel.com")
    app_id      = os.environ.get("EXOTEL_APP_ID", "")        # "1208054"
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "")

    if not all([api_key, api_token, account_sid, app_id]):
        raise HTTPException(
            status_code=500,
            detail="Exotel credentials not fully configured. "
                   "Check EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_ACCOUNT_SID, EXOTEL_APP_ID in .env"
        )

    # Exotel REST API endpoint for making calls
    url = (
        f"https://{api_key}:{api_token}{subdomain}"
        f"/v1/Accounts/{account_sid}/Calls/connect.json"
    )

    payload = {
        "From": req.to,          # Customer's number — Exotel calls this first
        "CallerId": req.from_,   # Your ExoPhone — shown as caller ID to customer
        # Exotel runs this app when the call connects:
        "Url": f"https://my.exotel.com/{account_sid}/exoml/start_voice/{app_id}",
        "StatusCallback": f"{webhook_base}/exotel/status",
        "StatusCallbackContentType": "application/json",
    }

    logger.info(f"Exotel outbound: calling {req.to} from {req.from_}")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, data=payload)

    if response.status_code not in (200, 201):
        logger.error(f"Exotel API error {response.status_code}: {response.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Exotel error {response.status_code}: {response.text}"
        )

    data = response.json()
    call = data.get("Call", {})
    logger.info(f"Exotel outbound call initiated: Sid={call.get('Sid')} Status={call.get('Status')}")

    return {
        "call_sid": call.get("Sid"),
        "status": call.get("Status"),
        "to": call.get("To"),
        "from": call.get("From"),
    }


@router.post("/status")
async def exotel_status_callback(request: Request):
    """
    Exotel posts call lifecycle events here.
    Set this URL in your Exotel app or outbound call payload.
    """
    try:
        # Exotel may send JSON or form data
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    logger.info(f"Exotel status callback: {body}")
    return {"ok": True}