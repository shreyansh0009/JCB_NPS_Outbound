"""
Exotel Voicebot Applet — WebSocket handler.

Exotel sends raw PCM (16-bit, 8 kHz, mono, little-endian) encoded in base64.
Message events from Exotel:
  connected  → WebSocket handshake done (no equivalent in Twilio)
  start      → stream begins; carries stream_sid + call_sid
  media      → base64 raw PCM audio chunk from the caller
  stop       → call ended

Messages we send back to Exotel:
  media      → base64 raw PCM audio for the agent's voice

The _ExotelAdapter class translates Exotel's message format into the
Twilio-style JSON that StreamingPipeline already understands, so the
pipeline itself needs zero changes.

Audio note: Exotel uses raw PCM 16-bit 8 kHz — NO mulaw conversion needed.
The base64 payload can be passed straight through in both directions.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.streaming_pipeline import StreamingPipeline

logger = logging.getLogger(__name__)

router = APIRouter()


def create_exotel_router(pipeline: StreamingPipeline) -> APIRouter:

    @router.websocket("/ws/exotel/{call_sid}")
    async def exotel_stream(websocket: WebSocket, call_sid: str):
        """
        Exotel Voicebot Applet connects here.
        In your Exotel app builder set the WebSocket URL to:
            wss://YOUR_SERVER/ws/exotel/{call_sid}
        or simply:
            wss://YOUR_SERVER/ws/exotel/call
        (Exotel substitutes the real call_sid automatically.)
        """
        await websocket.accept()
        logger.info(f"Exotel WebSocket connected: {call_sid}")
        adapted = _ExotelAdapter(websocket)
        try:
            await pipeline.run_call(call_sid, adapted)
        except WebSocketDisconnect:
            logger.info(f"Exotel WebSocket disconnected: {call_sid}")
        except Exception:
            logger.exception(f"Exotel WebSocket error: {call_sid}")

    return router


class _ExotelAdapter:
    """
    Translates Exotel Voicebot messages ↔ Twilio-style messages so that
    StreamingPipeline works without any modification.

    Key differences handled here:
    - Exotel sends an extra "connected" event before "start"
    - Exotel's start payload uses snake_case keys (stream_sid, call_sid)
      instead of Twilio's camelCase (streamSid, callSid)
    - Audio format is identical (base64 PCM) so payload passes through as-is
    """

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._started = False

    async def receive_text(self) -> str:
        msg_raw = await self._ws.receive_text()
        msg = json.loads(msg_raw)
        event = msg.get("event", "")

        # ── "connected" ──────────────────────────────────────────────────────
        # Exotel sends this first. Twilio has no equivalent.
        # The pipeline is waiting for "start", so skip this and get next msg.
        if event == "connected":
            logger.debug("Exotel: received 'connected' event, waiting for 'start'")
            return await self.receive_text()

        # ── "start" ──────────────────────────────────────────────────────────
        # Exotel:  { "event":"start", "stream_sid":"...", "call_sid":"...",
        #            "custom_parameters":{...} }
        # Twilio:  { "event":"start", "streamSid":"...",
        #            "start":{ "streamSid":"..." } }
        if event == "start":
            stream_sid = (
                msg.get("stream_sid")
                or msg.get("streamSid")
                or "exotel-stream-001"
            )
            call_sid = msg.get("call_sid", "")
            custom = msg.get("custom_parameters", {})
            self._started = True
            logger.info(f"Exotel: stream started — stream_sid={stream_sid} call_sid={call_sid}")
            return json.dumps({
                "event": "start",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                    "callSid": call_sid,
                    "customParameters": custom,
                },
            })

        # ── "media" (incoming audio from caller) ─────────────────────────────
        # Exotel:  { "event":"media", "media":{ "payload":"<base64 PCM>" } }
        # Twilio:  { "event":"media", "media":{ "payload":"<base64 mulaw>" } }
        # Audio format is raw PCM in both cases for Exotel; payload passes through.
        if event == "media":
            payload = msg.get("media", {}).get("payload", "")
            return json.dumps({
                "event": "media",
                "media": {"payload": payload},
            })

        # ── "stop" ───────────────────────────────────────────────────────────
        if event == "stop":
            logger.info("Exotel: received 'stop' event — call ended")
            return json.dumps({"event": "stop"})

        # Unknown event — pass through unchanged
        logger.debug(f"Exotel: unknown event '{event}', passing through")
        return msg_raw

    async def send_text(self, text: str) -> None:
        """
        StreamingPipeline sends Twilio-format JSON with base64 audio.
        Translate back to Exotel's media format.
        """
        msg = json.loads(text)
        if msg.get("event") == "media":
            # Payload is base64 PCM — Exotel accepts the same format
            exotel_msg = {
                "event": "media",
                "media": {
                    "payload": msg["media"]["payload"]
                },
            }
            await self._ws.send_text(json.dumps(exotel_msg))
        # Ignore non-media messages (marks, clears, etc.)