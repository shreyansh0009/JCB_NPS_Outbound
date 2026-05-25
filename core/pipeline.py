"""
AudioPipeline: glues STT → Orchestrator → TTS together.

Call flow for a single user audio chunk:
  1. Accumulate audio until VAD signals end-of-speech
  2. Send audio to STT -> get transcript
  3. Send transcript to Orchestrator.process() -> get AgentResponse
  4. Send response text to TTS -> get audio bytes
  5. Return audio bytes to the transport layer (WebSocket / Twilio)
"""
from __future__ import annotations

import audioop
import logging
from dataclasses import dataclass, field

from core.orchestrator import Orchestrator
from core.session import CallSession
from providers.stt.base import BaseSTT
from providers.tts.base import BaseTTS

logger = logging.getLogger(__name__)

# Minimum audio energy to consider a chunk as speech (energy-based VAD)
SILENCE_THRESHOLD = 200
# How many consecutive silent chunks before we consider speech ended
SILENCE_CHUNKS_TO_END = 20  # ~0.5s at 25 chunks/sec


@dataclass
class PipelineState:
    """Per-session mutable state for the audio pipeline."""
    audio_buffer: bytes = b""
    silent_chunks: int = 0
    is_speaking: bool = False
    session: CallSession = field(default_factory=CallSession)


class AudioPipeline:
    def __init__(self, stt: BaseSTT, tts: BaseTTS, orchestrator: Orchestrator):
        self.stt = stt
        self.tts = tts
        self.orchestrator = orchestrator
        self._sessions: dict[str, PipelineState] = {}

    def create_session(self, call_sid: str) -> CallSession:
        """Create a new call session and wire it up to the pipeline."""
        session = self.orchestrator.start_session()
        self._sessions[call_sid] = PipelineState(session=session)
        logger.info(f"Pipeline session created: call_sid={call_sid}, session_id={session.session_id}")
        return session

    def end_session(self, call_sid: str) -> None:
        self._sessions.pop(call_sid, None)
        logger.info(f"Pipeline session ended: call_sid={call_sid}")

    async def process_audio_chunk(
        self, call_sid: str, chunk: bytes, sample_rate: int = 8000
    ) -> bytes | None:
        """
        Feed raw PCM audio (16-bit, mono).
        Returns TTS audio bytes when a full utterance is processed, else None.
        """
        state = self._sessions.get(call_sid)
        if not state:
            logger.warning(f"No pipeline state for call_sid={call_sid}")
            return None

        # Energy-based voice activity detection
        rms = audioop.rms(chunk, 2)  # 2 bytes per sample (16-bit)
        is_speech = rms > SILENCE_THRESHOLD

        if is_speech:
            state.audio_buffer += chunk
            state.is_speaking = True
            state.silent_chunks = 0
        elif state.is_speaking:
            state.silent_chunks += 1
            state.audio_buffer += chunk  # keep trailing silence for cleaner STT

            if state.silent_chunks >= SILENCE_CHUNKS_TO_END:
                # End of utterance — process it
                audio_data = state.audio_buffer
                state.audio_buffer = b""
                state.is_speaking = False
                state.silent_chunks = 0

                return await self._process_utterance(call_sid, audio_data, sample_rate, state)

        return None

    async def _process_utterance(
        self, call_sid: str, audio: bytes, sample_rate: int, state: PipelineState
    ) -> bytes | None:
        try:
            # STT
            transcript = await self.stt.transcribe(audio, sample_rate)
            if not transcript.strip():
                logger.debug(f"Empty transcript for call_sid={call_sid}, skipping")
                return None

            logger.info(f"[{call_sid}] User: {transcript}")

            # Language detection — update session language & instruction
            lang_code, switched = state.session.update_language(transcript)
            state.session.set("language", lang_code)
            _LANG_INSTRUCTIONS = {
                "hi": "You MUST respond only in Hindi (हिंदी). Do not use English unless the user explicitly asks.",
                "bn": "You MUST respond only in Bengali (বাংলা). Do not use English unless the user explicitly asks.",
                "te": "You MUST respond only in Telugu (తెలుగు). Do not use English unless the user explicitly asks.",
                "mr": "You MUST respond only in Marathi (मराठी). Do not use English unless the user explicitly asks.",
                "ta": "You MUST respond only in Tamil (தமிழ்). Do not use English unless the user explicitly asks.",
                "gu": "You MUST respond only in Gujarati (ગુજરાતી). Do not use English unless the user explicitly asks.",
                "kn": "You MUST respond only in Kannada (ಕನ್ನಡ). Do not use English unless the user explicitly asks.",
                "pa": "You MUST respond only in Punjabi (ਪੰਜਾਬੀ). Do not use English unless the user explicitly asks.",
                "ml": "You MUST respond only in Malayalam (മലയാളം). Do not use English unless the user explicitly asks.",
                "or": "You MUST respond only in Odia (ଓଡ଼ିଆ). Do not use English unless the user explicitly asks.",
                "en": "You MUST respond only in English.",
            }
            state.session.set("language_instruction", _LANG_INSTRUCTIONS.get(lang_code, ""))
            if switched:
                logger.info(f"[{call_sid}] Language switched → {lang_code}")
                if hasattr(self.tts, "set_language"):
                    self.tts.set_language(lang_code)

            # Agent
            response = await self.orchestrator.process(transcript, state.session)

            logger.info(f"[{call_sid}] Agent ({state.session.current_agent}): {response.text}")

            # TTS
            audio_out = await self.tts.synthesize(response.text)
            return audio_out

        except Exception:
            logger.exception(f"Pipeline error for call_sid={call_sid}")
            return None
