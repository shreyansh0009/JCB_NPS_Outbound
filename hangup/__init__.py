import asyncio
import struct

_AS_HANGUP_FRAME = struct.pack("!BH", 0xFF, 0)


async def send_hangup_frame(writer: asyncio.StreamWriter) -> None:
    """Send AudioSocket HANGUP (0xFF) frame to Asterisk, then flush."""
    writer.write(_AS_HANGUP_FRAME)
    await writer.drain()
