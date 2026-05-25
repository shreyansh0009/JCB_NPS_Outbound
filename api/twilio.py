"""
Twilio webhook handler.

Twilio calls these endpoints when:
  1. An inbound call arrives  -> /twilio/inbound  (returns TwiML to connect WebSocket)
  2. A call status changes    -> /twilio/status
  3. Voicemail is left        -> /twilio/voicemail

TwiML response for inbound call connects Twilio to our WebSocket media stream.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])


def _twiml_connect_stream(call_sid: str, websocket_url: str) -> str:
    """
    Return TwiML that connects the call to our WebSocket media stream.
    Twilio will stream audio to/from the WebSocket endpoint.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{websocket_url}/ws/call/{call_sid}">
      <Parameter name="call_sid" value="{call_sid}"/>
    </Stream>
  </Connect>
</Response>"""


@router.post("/inbound")
async def inbound_call(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
):
    """
    Twilio calls this when a new inbound call arrives.
    We respond with TwiML to connect it to our WebSocket.
    """
    logger.info(f"Inbound call: CallSid={CallSid}, From={From}, To={To}")

    websocket_base = os.environ.get("WEBSOCKET_BASE_URL", "wss://your-ec2-host.com")
    twiml = _twiml_connect_stream(CallSid, websocket_base)

    return Response(content=twiml, media_type="application/xml")


@router.post("/outbound")
async def outbound_call_connected(
    request: Request,
    CallSid: str = Form(...),
    CallStatus: str = Form(default=""),
):
    """
    Called when an outbound call is answered.
    Returns TwiML to connect to our WebSocket for outbound calls.
    """
    logger.info(f"Outbound call connected: CallSid={CallSid}, status={CallStatus}")

    websocket_base = os.environ.get("WEBSOCKET_BASE_URL", "wss://your-ec2-host.com")
    twiml = _twiml_connect_stream(CallSid, websocket_base)

    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def call_status(
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
):
    """Twilio status callback — log call lifecycle events."""
    logger.info(f"Call status update: CallSid={CallSid}, status={CallStatus}")
    return {"ok": True}
