"""
SchedulerAgent — NPS escalation check (Step 7) and handoff to closer.

For outbound NPS calls the path is deterministic:
  score 8-10, no complaint  -> skip LLM, go straight to closer (silent).
  score <=7 or complaint    -> call LLM once for Step 7 escalation offer,
                               then force [HANDOFF:closer] regardless.

MCP tools (from DMS) are only used on inbound paths:
  get_service_slots(date, service_type) -> available times
  book_service_appointment(name, phone, date, time, service_type) -> confirmation
"""
from __future__ import annotations

import re
from typing import Optional

from core.agent import (
    BaseAgent,
    AgentResponse,
    HandoffSignal,
    _HANDOFF_RE,
    _REPLY_CLEAN_RE,
    _SENT_BOUNDARY_RE,
)
from core.session import CallSession

# Hindi + English number words -> integer for NPS score extraction
_HINDI_NUMBERS: dict[str, int] = {
    # Hindi
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4,
    "पाँच": 5, "पांच": 5, "छह": 6, "छः": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    # English words (customer may speak rating in English)
    "ten": 10, "nine": 9, "eight": 8, "seven": 7,
    "six": 6, "five": 5, "four": 4, "three": 3,
    "two": 2, "one": 1,
}

# Keywords indicating a complaint was raised
_COMPLAINT_RE = re.compile(
    r'\b(complaint|problem|issue|damage|broken|defect|shikayat|samasya'
    r'|शिकायत|समस्या|परेशानी|खराब|नुकसान)\b',
    re.IGNORECASE,
)


_HINDI_ONLY = {k: v for k, v in _HINDI_NUMBERS.items() if not k.isascii()}
_ENGLISH_WORDS = {k: v for k, v in _HINDI_NUMBERS.items() if k.isascii()}


def _extract_nps_score(transcript: str, messages: list[dict]) -> Optional[int]:
    """Return NPS score (1-10) from transcript then recent session history."""
    # 1. Check the transcript itself
    # Hindi number words (substring match is safe — no overlap)
    for word, val in _HINDI_ONLY.items():
        if word in transcript:
            return val
    # English number words (word-boundary match to avoid "none"→"one" false positives)
    for word, val in _ENGLISH_WORDS.items():
        if re.search(rf'\b{word}\b', transcript, re.IGNORECASE):
            return val
    # Bare digit
    m = re.search(r'\b(10|[1-9])\b', transcript)
    if m:
        return int(m.group(1))

    # 2. Scan last 8 assistant messages for the service readback pattern
    #    e.g. "दस में से आठ अंक" or "8 out of 10"
    for msg in reversed(messages[-8:]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        for word, val in _HINDI_ONLY.items():
            if word in content and "दस में से" in content:
                return val
        eng = re.search(r'\b(10|[1-9])\s*out\s*of\s*10\b', content, re.IGNORECASE)
        if eng:
            return int(eng.group(1))
    return None


def _has_complaint(session: CallSession, messages: list[dict]) -> bool:
    """Return True if a complaint or escalation was raised during this call."""
    if session.get("complaint_raised") or session.get("reported_issue"):
        return True
    for msg in messages[-10:]:
        if msg.get("role") == "user" and _COMPLAINT_RE.search(msg.get("content", "")):
            return True
    return False


class SchedulerAgent(BaseAgent):
    name = "scheduler"
    can_handoff_to = ["closer"]

    # ── Non-streaming path (used by orchestrator.process and tests) ────────────

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            return await self._handle_outbound(transcript, session)

        reply    = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply
        clean    = _REPLY_CLEAN_RE.sub("", reply).strip()

        if handoff:
            return AgentResponse(
                text=clean,
                handoff=HandoffSignal(
                    target=handoff,
                    data=self._handoff_data(session),
                ),
            )
        if end_call:
            return AgentResponse(text=clean, end_call=True)
        return AgentResponse(text=clean)

    # ── Streaming path (used by streaming_pipeline via orchestrator.stream_process) ──

    async def stream_handle(self, transcript: str, session):
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            async for item in self._stream_outbound(transcript, session):
                yield item
            return
        async for item in super().stream_handle(transcript, session):
            yield item

    # ── Outbound deterministic logic ───────────────────────────────────────────

    async def _handle_outbound(self, transcript: str, session: CallSession) -> AgentResponse:
        """Non-streaming outbound NPS path."""
        history   = session.history
        score     = session.get("nps_rating") or _extract_nps_score(transcript, history)
        complaint = _has_complaint(session, history)
        if score is not None and not session.get("nps_rating"):
            session.set("nps_rating", score)

        if score is not None and score >= 8 and not complaint:
            return AgentResponse(
                text="",
                handoff=HandoffSignal(target="closer", data=self._handoff_data(session)),
            )

        reply    = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        clean    = _REPLY_CLEAN_RE.sub("", reply).strip()
        target   = handoff if handoff else "closer"
        return AgentResponse(
            text=clean,
            handoff=HandoffSignal(target=target, data=self._handoff_data(session)),
        )

    async def _stream_outbound(self, transcript: str, session):
        """
        Streaming outbound NPS path.

        Score 8-10, no complaint: yield a silent handoff to closer immediately.
        Score <=7 or complaint: stream LLM once for the escalation question,
        then always force [HANDOFF:closer] so the closer handles the goodbye.
        """
        history   = session.history
        score     = session.get("nps_rating") or _extract_nps_score(transcript, history)
        complaint = _has_complaint(session, history)
        if score is not None and not session.get("nps_rating"):
            session.set("nps_rating", score)

        if score is not None and score >= 8 and not complaint:
            yield None, AgentResponse(
                text="",
                handoff=HandoffSignal(target="closer", data=self._handoff_data(session)),
            )
            return

        # Low rating or complaint: run Step 7 escalation question via LLM
        transcript  = self._preprocess_transcript(transcript, session)
        rag_context = await self._retrieve_context(transcript)
        msgs        = self._build_messages(session, transcript, rag_context)
        self._log_token_estimate(msgs)

        full_reply = ""
        buf        = ""
        session.set("_partial_assistant", "")

        async for token in self.llm.stream_chat(msgs):
            full_reply += token
            buf        += token
            session.set("_partial_assistant", full_reply)

            while True:
                sm = _SENT_BOUNDARY_RE.search(buf)
                if sm:
                    sentence = buf[:sm.start()]
                    buf      = buf[sm.end():]
                    clean    = _REPLY_CLEAN_RE.sub("", sentence).strip()
                    if clean:
                        yield clean, None
                    continue
                if len(buf) > 40:
                    comma_pos = buf.rfind(", ")
                    if comma_pos > 20:
                        clause = buf[:comma_pos + 1]
                        buf    = buf[comma_pos + 2:]
                        clean  = _REPLY_CLEAN_RE.sub("", clause).strip()
                        if clean:
                            yield clean, None
                        continue
                break

        if buf.strip():
            clean = _REPLY_CLEAN_RE.sub("", buf).strip()
            if clean:
                yield clean, None

        session.add_message("user",      transcript)
        session.add_message("assistant", full_reply)
        session.set("_partial_assistant", "")

        # Force handoff to closer regardless of whether LLM emitted [HANDOFF:closer]
        hm     = _HANDOFF_RE.search(full_reply)
        target = hm.group(1) if (hm and hm.group(1) in self.can_handoff_to) else "closer"
        yield None, AgentResponse(
            text=_REPLY_CLEAN_RE.sub("", full_reply).strip(),
            handoff=HandoffSignal(target=target, data=self._handoff_data(session)),
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _handoff_data(self, session: CallSession) -> dict:
        return {
            "customer_name":   session.get("customer_name", ""),
            "customer_mobile": session.get("customer_mobile", ""),
            "language":        session.get("language", ""),
        }

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
