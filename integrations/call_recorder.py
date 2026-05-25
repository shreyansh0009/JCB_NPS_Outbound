"""
CallRecorder — records both sides of a call and uploads to Cloudinary.

Self-contained module with no imports from core/ — portable across projects.
Just needs httpx for the Cloudinary upload.

Usage:
    recorder = CallRecorder(call_sid)

    # During call — feed raw PCM (slin16, 8kHz, mono, 320-byte frames):
    recorder.record_incoming(payload)   # caller audio
    recorder.record_outgoing(chunk)     # agent TTS audio

    # After call ends:
    wav_path = recorder.finalize()                       # mix + write WAV
    url = await recorder.upload_to_cloudinary(settings)  # upload → public URL
    recorder.cleanup()                                   # delete local file
"""
from __future__ import annotations

import hashlib
import logging
import os
import struct
import time
import wave
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"


class CallRecorder:
    """
    Records incoming (caller) and outgoing (agent) audio streams,
    mixes them into a single WAV, and uploads to Cloudinary.
    """

    def __init__(
        self,
        call_sid: str,
        sample_rate: int = 8000,
        sample_width: int = 2,
        channels: int = 1,
    ):
        self.call_sid = call_sid
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        # Store (timestamp, pcm_bytes) pairs for time-aligned mixing
        self._incoming_chunks: list[tuple[float, bytes]] = []
        self._outgoing_chunks: list[tuple[float, bytes]] = []
        self._wav_path: Optional[Path] = None
        self._finalized = False

    def record_incoming(self, pcm: bytes) -> None:
        """Append caller audio PCM chunk with timestamp."""
        if not self._finalized:
            self._incoming_chunks.append((time.monotonic(), pcm))

    def record_outgoing(self, pcm: bytes) -> None:
        """Append agent TTS audio PCM chunk with timestamp."""
        if not self._finalized:
            self._outgoing_chunks.append((time.monotonic(), pcm))

    def _rebuild_timeline_sequential(self, chunks: list[tuple[float, bytes]]) -> bytearray:
        """
        Place a continuous real-time stream at its correct start offset,
        then append chunks sequentially (no timestamp jitter between chunks).
        Used for caller audio which arrives as a steady real-time stream.
        """
        if not chunks:
            return bytearray()

        bytes_per_sec = self.sample_rate * self.sample_width * self.channels

        # Use first chunk's timestamp to calculate the start offset
        start_offset = int((chunks[0][0] - self._start_time) * bytes_per_sec)
        start_offset -= start_offset % self.sample_width

        # Concatenate all chunks sequentially (preserves clean audio)
        pcm_data = bytearray()
        for _, pcm in chunks:
            pcm_data.extend(pcm)

        buf = bytearray(start_offset) + pcm_data
        return buf

    def _rebuild_timeline_timestamped(self, chunks: list[tuple[float, bytes]]) -> bytearray:
        """
        Sequential append within utterances (chunks arriving in a fast burst),
        timestamp-based jumps only for genuine inter-utterance silence gaps.

        TTS chunks are written to the socket in rapid bursts (faster than real-time),
        so consecutive chunk timestamps are milliseconds apart even though the audio
        represents 20ms per frame.  We only insert silence when the wall-clock gap
        between two consecutive chunks exceeds their PCM duration by > GAP_THRESHOLD,
        which indicates a real pause between utterances.
        """
        if not chunks:
            return bytearray()

        bytes_per_sec = self.sample_rate * self.sample_width * self.channels
        GAP_THRESHOLD = 0.20  # 200 ms — clear inter-utterance pause

        # Start at the correct offset for this stream
        start_offset = int((chunks[0][0] - self._start_time) * bytes_per_sec)
        start_offset -= start_offset % self.sample_width
        buf = bytearray(start_offset)

        for i, (ts, pcm) in enumerate(chunks):
            if i > 0:
                prev_ts, prev_pcm = chunks[i - 1]
                wall_gap = ts - prev_ts
                pcm_duration = len(prev_pcm) / bytes_per_sec
                # Real silence gap: wall time far exceeded the PCM duration
                if wall_gap > pcm_duration + GAP_THRESHOLD:
                    silence_bytes = int((wall_gap - pcm_duration) * bytes_per_sec)
                    silence_bytes -= silence_bytes % self.sample_width
                    buf.extend(b"\x00" * silence_bytes)
            buf.extend(pcm)

        return buf

    def finalize(self) -> Path:
        """
        Mix incoming + outgoing audio and write a WAV file.

        Returns the path to the WAV file.
        """
        self._finalized = True
        os.makedirs(_RECORDINGS_DIR, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{self.call_sid}_{ts}.wav"
        self._wav_path = _RECORDINGS_DIR / filename

        # Use the earliest timestamp across both streams as the shared t=0
        first_ts = []
        if self._incoming_chunks:
            first_ts.append(self._incoming_chunks[0][0])
        if self._outgoing_chunks:
            first_ts.append(self._outgoing_chunks[0][0])
        self._start_time = min(first_ts) if first_ts else time.monotonic()

        # Incoming (caller): continuous real-time stream → sequential append at correct start offset
        # Outgoing (agent TTS): arrives in bursts with gaps → timestamp-based placement
        incoming_buf = self._rebuild_timeline_sequential(self._incoming_chunks)
        outgoing_buf = self._rebuild_timeline_timestamped(self._outgoing_chunks)

        # Pad shorter buffer to match longer
        max_len = max(len(incoming_buf), len(outgoing_buf))
        if len(incoming_buf) < max_len:
            incoming_buf.extend(b"\x00" * (max_len - len(incoming_buf)))
        if len(outgoing_buf) < max_len:
            outgoing_buf.extend(b"\x00" * (max_len - len(outgoing_buf)))

        # Mix: average the two 16-bit PCM streams sample-by-sample
        num_samples = max_len // self.sample_width
        mixed = bytearray(max_len)

        for i in range(num_samples):
            offset = i * 2
            s_in = struct.unpack_from("<h", incoming_buf, offset)[0]
            s_out = struct.unpack_from("<h", outgoing_buf, offset)[0]
            # Average with clamp to int16 range
            mixed_sample = max(-32768, min(32767, (s_in + s_out) // 2))
            struct.pack_into("<h", mixed, offset, mixed_sample)

        # Write WAV
        with wave.open(str(self._wav_path), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.sample_width)
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(mixed))

        # Free memory
        self._incoming_chunks.clear()
        self._outgoing_chunks.clear()

        duration_s = num_samples / self.sample_rate
        logger.info(
            f"[{self.call_sid}] Recording finalized: {filename} "
            f"({duration_s:.1f}s, {max_len} bytes)"
        )
        return self._wav_path

    async def upload_to_cloudinary(self, settings) -> str:
        """
        Upload the WAV file to Cloudinary and return the public URL.

        Uses the Cloudinary Upload API directly via httpx (no SDK needed).
        Settings must have: cloudinary_cloud_name, cloudinary_api_key, cloudinary_api_secret.
        """
        if not self._wav_path or not self._wav_path.exists():
            logger.error(f"[{self.call_sid}] No WAV file to upload")
            return ""

        if not settings.cloudinary_cloud_name:
            logger.debug("Cloudinary not configured — skipping upload")
            return ""

        # Build signed upload params
        timestamp = str(int(time.time()))
        folder = "call_recordings"
        public_id = self._wav_path.stem  # e.g. "callsid_20260408_180000"

        # Cloudinary signature: sort params alphabetically, join with &, append api_secret
        params_to_sign = f"folder={folder}&public_id={public_id}&timestamp={timestamp}"
        signature = hashlib.sha1(
            (params_to_sign + settings.cloudinary_api_secret).encode()
        ).hexdigest()

        url = f"https://api.cloudinary.com/v1_1/{settings.cloudinary_cloud_name}/video/upload"

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(self._wav_path, "rb") as f:
                resp = await client.post(
                    url,
                    data={
                        "api_key": settings.cloudinary_api_key,
                        "timestamp": timestamp,
                        "signature": signature,
                        "folder": folder,
                        "public_id": public_id,
                        "resource_type": "video",  # Cloudinary uses "video" for audio files
                    },
                    files={"file": (self._wav_path.name, f, "audio/wav")},
                )
                resp.raise_for_status()
                result = resp.json()

        secure_url = result.get("secure_url", "")
        logger.info(f"[{self.call_sid}] Recording uploaded → {secure_url}")
        return secure_url

    def cleanup(self) -> None:
        """Delete the local WAV file after successful upload."""
        if self._wav_path and self._wav_path.exists():
            try:
                self._wav_path.unlink()
                logger.debug(f"[{self.call_sid}] Local recording deleted")
            except OSError:
                pass
