"""
WebSocket endpoints — streaming pipeline.

/ws/call/{call_sid}  Twilio Media Stream (mulaw 8kHz) → StreamingPipeline
/ws/raw/{call_sid}   Raw PCM (browser/LiveKit) → adapted to StreamingPipeline
"""
from __future__ import annotations

import audioop
import base64
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.streaming_pipeline import StreamingPipeline

logger = logging.getLogger(__name__)

router = APIRouter()


def create_websocket_router(pipeline: StreamingPipeline) -> APIRouter:

    @router.websocket("/ws/call/{call_sid}")
    async def twilio_stream(websocket: WebSocket, call_sid: str):
        """Twilio Media Stream — delegates to StreamingPipeline."""
        await websocket.accept()
        logger.info(f"Twilio WebSocket connected: {call_sid}")
        try:
            await pipeline.run_call(call_sid, websocket)
        except WebSocketDisconnect:
            logger.info(f"Twilio WebSocket disconnected: {call_sid}")
        except Exception:
            logger.exception(f"Twilio WebSocket error: {call_sid}")

    @router.websocket("/ws/raw/{call_sid}")
    async def raw_pcm_stream(websocket: WebSocket, call_sid: str):
        """
        Raw PCM WebSocket for browser/LiveKit.
        Client sends 16-bit PCM (16kHz mono) binary frames.
        Adapter converts to/from Twilio message format so StreamingPipeline
        handles it without any changes.
        """
        await websocket.accept()
        logger.info(f"Raw WebSocket connected: {call_sid}")
        adapted = _RawPCMAdapter(websocket)
        try:
            await pipeline.run_call(call_sid, adapted)
        except WebSocketDisconnect:
            logger.info(f"Raw WebSocket disconnected: {call_sid}")
        except Exception:
            logger.exception(f"Raw WebSocket error: {call_sid}")

    return router


class _RawPCMAdapter:
    """Wraps a raw PCM WebSocket to look like a Twilio media stream."""

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._state = None  # for ratecv
        self._started = False  # track whether we've emitted the "start" event yet

    async def receive_text(self) -> str:
        # BUG FIX: The streaming pipeline waits for a Twilio "start" event to
        # set stream_sid before it plays audio or sends responses. Without it,
        # stream_sid stays empty and ALL audio output is silently dropped.
        # Emit a synthetic "start" event on the first call so the pipeline
        # unlocks and the welcome message / responses can be sent.
        if not self._started:
            self._started = True
            return json.dumps({
                "event": "start",
                "streamSid": "raw-stream-001",
                "start": {"streamSid": "raw-stream-001"},
            })

        pcm_16k = await self._ws.receive_bytes()
        pcm_8k, self._state = audioop.ratecv(pcm_16k, 2, 1, 16000, 8000, self._state)
        mulaw = audioop.lin2ulaw(pcm_8k, 2)
        payload = base64.b64encode(mulaw).decode()
        return json.dumps({"event": "media", "media": {"payload": payload}})

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        if msg.get("event") == "media":
            mulaw = base64.b64decode(msg["media"]["payload"])
            pcm_8k = audioop.ulaw2lin(mulaw, 2)
            pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
            await self._ws.send_bytes(pcm_16k)
