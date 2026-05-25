"""
Asterisk AudioSocket — TCP server handler.

AudioSocket is a binary protocol over TCP. Asterisk sends/receives raw slin16
PCM audio frames (signed 16-bit LE, 8 kHz, mono).

Frame format:
    [type: 1 byte][length: 2 bytes big-endian][payload: N bytes]

Type values:
    0x00  UUID    — 16-byte UUID identifying the call (sent once at connection start)
    0x10  SLIN    — Audio data (slin16 PCM)
    0xFF  HANGUP  — Call ended

Sprint 2: CallGate integration.
  When the worker is at max_concurrent_calls capacity, new connections are held
  in a bounded queue (up to queue_timeout_s). If no slot opens in time, a HANGUP
  frame is sent to Asterisk and the TCP connection is closed.

Usage:
    In Asterisk dialplan:
        exten => _X.,1,Answer()
         same => n,Wait(0.2)
         same => n,AudioSocket(${UNIQUEID},127.0.0.1:9093)
         same => n,Hangup()
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import uuid

from core.call_queue import CallGate
from core.tracing import aspan

logger = logging.getLogger(__name__)

# AudioSocket frame types
AS_TYPE_UUID   = 0x00
AS_TYPE_SLIN   = 0x10
AS_TYPE_HANGUP = 0xFF


def _make_hangup_frame() -> bytes:
    """Build an AudioSocket HANGUP frame to signal Asterisk to end the call."""
    return struct.pack("!BH", AS_TYPE_HANGUP, 0)


async def start_audiosocket_server(
    pipeline,
    host: str = "0.0.0.0",
    port: int = 9093,
    call_gate: CallGate | None = None,
    max_call_duration_s: float = 600.0,
):
    """
    Start the AudioSocket TCP server.

    Args:
        pipeline:            StreamingPipeline instance (one call per connection)
        host:                Bind host
        port:                Bind port
        call_gate:           Optional CallGate for concurrency limiting.
                             Pass None to accept unlimited connections (dev mode).
        max_call_duration_s: Hard cap per call. Sockets that overrun are force-closed.
    """

    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info(f"AudioSocket connection from {peer}")

        # ── Read first frame — should be UUID ────────────────────────────────
        call_sid = str(uuid.uuid4())  # fallback
        try:
            header = await asyncio.wait_for(reader.readexactly(3), timeout=5.0)
            frame_type = header[0]
            length = int.from_bytes(header[1:3], "big")
            if length > 0:
                payload = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
            else:
                payload = b""

            if frame_type == AS_TYPE_UUID and len(payload) == 16:
                call_sid = str(uuid.UUID(bytes=payload))
                logger.info(f"AudioSocket UUID: {call_sid}")
            elif frame_type == AS_TYPE_UUID:
                call_sid = payload.decode("utf-8", errors="replace").strip("\x00")
                logger.info(f"AudioSocket UUID (string): {call_sid}")
        except Exception as e:
            logger.warning(f"AudioSocket: failed to read UUID frame: {e}")

        # ── CallGate: enforce max concurrency ─────────────────────────────────
        if call_gate is not None:
            async with call_gate.acquire(call_sid) as accepted:
                if not accepted:
                    # At capacity and timed out — send HANGUP to Asterisk
                    logger.warning(f"[CallGate] Sending HANGUP to Asterisk for rejected call {call_sid}")
                    try:
                        writer.write(_make_hangup_frame())
                        await writer.drain()
                    except Exception:
                        pass
                    finally:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass
                    return

                # Slot acquired — run the call inside the gate context
                await _run_call(pipeline, call_sid, reader, writer, max_call_duration_s)
        else:
            # No gate — unlimited connections (development mode)
            await _run_call(pipeline, call_sid, reader, writer, max_call_duration_s)


    server = await asyncio.start_server(handle_connection, host, port)
    for sock in server.sockets:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    addr = server.sockets[0].getsockname() if server.sockets else (host, port)
    logger.info(f"AudioSocket TCP server listening on {addr[0]}:{addr[1]}")
    return server


async def _run_call(
    pipeline,
    call_sid: str,
    reader,
    writer,
    max_duration_s: float = 600.0,
) -> None:
    """Execute one call through the pipeline, ensuring the writer is always closed.

    A hard duration cap prevents hung sockets from holding a CallGate slot for
    Linux TCP keep-alive's ~2 hour default. Idle (no-audio) timeout is enforced
    inside the pipeline via Deepgram silence detection.
    """
    async with aspan("call.run", attrs={"call.sid": call_sid}):
        try:
            await asyncio.wait_for(
                pipeline.run_audiosocket_call(call_sid, reader, writer),
                timeout=max_duration_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"AudioSocket: call {call_sid} exceeded max_duration={max_duration_s}s — force closing"
            )
        except Exception:
            logger.exception(f"AudioSocket: error during call {call_sid}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"AudioSocket: call ended {call_sid}")
