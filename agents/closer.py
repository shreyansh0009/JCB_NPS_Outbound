"""
CloserAgent: wraps up the call.

Confirms service/complaint booking, collects feedback,
and ends the call warmly.
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


# Urdu/Persian farewell words that must never appear in Hindalco calls.
# Replace each with the approved Hindi equivalent.
_URDU_REPLACEMENTS: list[tuple[str, str]] = [
    ("अलविदा", "आपका दिन शुभ हो"),
    ("alvida", "have a great day"),
    ("खुदा हाफ़िज़", "आपका दिन शुभ हो"),
    ("खुदा हाफिज", "आपका दिन शुभ हो"),
    ("खुदा हाफ़िज", "आपका दिन शुभ हो"),
    ("khuda hafiz", "have a great day"),
    ("मेहरबानी", "शुक्रिया"),
    ("meherbani", "thank you"),
]


def _sanitize_urdu(text: str) -> str:
    """Replace banned Urdu farewell words with approved Hindi equivalents."""
    result = text
    for urdu, hindi in _URDU_REPLACEMENTS:
        result = re.sub(re.escape(urdu), hindi, result, flags=re.IGNORECASE)
    return result


def _is_outbound(session: CallSession) -> bool:
    return (
        session.get("direction") == "outbound"
        or session.get("support_domain") == "outbound"
    )


_OUTBOUND_CLOSING = {
    "hi": "आपके समय और बहुमूल्य feedback के लिए बहुत-बहुत धन्यवाद। आपका दिन शुभ हो।",
    "en": "Thank you so very much for your time and your valuable feedback. Have a wonderful day.",
}


class CloserAgent(BaseAgent):
    name = "closer"
    can_handoff_to = ["screener"]

    async def stream_handle(self, transcript: str, session: CallSession):
        if _is_outbound(session):
            # Deterministic outbound path: no LLM call, no hallucination possible.
            # The closing line is fixed regardless of conversation history.
            lang = session.get("language", "hi") or "hi"
            closing = _OUTBOUND_CLOSING.get(lang, _OUTBOUND_CLOSING["hi"])
            yield closing, None
            yield None, AgentResponse(text=closing, end_call=True)
            return

        async for sentence, resp in super().stream_handle(transcript, session):
            yield sentence, resp

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        # Outbound: deterministic — no LLM call needed.
        if _is_outbound(session):
            lang = session.get("language", "hi") or "hi"
            closing = _OUTBOUND_CLOSING.get(lang, _OUTBOUND_CLOSING["hi"])
            return AgentResponse(text=closing, end_call=True)

        language = session.get("language", session.current_language)
        reply = await self._chat(session, transcript)
        handoff = self._parse_handoff(reply)

        # Strip ALL control tags
        clean_reply = re.sub(
            r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP|TOOL):[^\]]*\]|\[END_CALL\]",
            "", reply
        ).strip()

        # Remove any Urdu/Persian words the LLM may have generated
        clean_reply = _sanitize_urdu(clean_reply)

        # Inbound: end only when caller signals done or LLM emits [END_CALL]
        has_end_call = "[END_CALL]" in reply
        caller_done = self._caller_is_done(transcript)

        if caller_done or has_end_call:
            if language == "hi":
                goodbye = (
                    "गोदरेज कस्टमर केयर में कॉल करने के लिए धन्यवाद! "
                    "हम हमेशा आपकी सहायता के लिए यहाँ हैं — आपका दिन शुभ हो!"
                )
            else:
                goodbye = (
                    "Thank you for calling Godrej Customer Care! "
                    "We're always here to help — have a wonderful day!"
                )
            return AgentResponse(text=goodbye, end_call=True)

        return AgentResponse(text=clean_reply, end_call=False)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _contains_goodbye_phrase(self, text: str) -> bool:
        """Phrases that only appear when the call is genuinely wrapping up."""
        lower = text.lower()
        return any(p in lower for p in [
            "have a fantastic day", "have a great day", "have a wonderful day",
            "goodbye", "take care", "drive safe",
            "looking forward to working with you",
            "calling godrej", "choosing godrej",
            "शुभ दिन", "धन्यवाद",
        ])

    def _contains_offer_help(self, text: str) -> bool:
        """Detect "anything else I can help" — means we are still mid-conversation."""
        lower = text.lower()
        return any(p in lower for p in [
            "anything else i can help",
            "anything else i can assist",
            "is there anything else",
            "anything else you",
        ])

    def _strip_after_offer_help(self, text: str) -> str:
        """Keep everything up to and including the "anything else?" question."""
        patterns = [
            "anything else i can help",
            "anything else i can assist",
            "is there anything else",
            "anything else you",
        ]
        lower = text.lower()
        cut = len(text)
        for p in patterns:
            idx = lower.find(p)
            if idx != -1:
                # Include the full sentence containing the pattern
                end = text.find("?", idx)
                if end != -1:
                    cut = min(cut, end + 1)
        return text[:cut].strip()

    def _caller_is_done(self, transcript: str) -> bool:
        lower = transcript.lower().strip()
        done_phrases = [
            "no", "nope", "nothing", "that's all", "thats all", "that's it",
            "thats it", "no thanks", "no thank you", "i'm good", "im good",
            "bye", "goodbye", "talk later", "have a good", "take care",
            "nahi", "bas", "bas itna hi", "theek hai", "dhanyavad", "shukriya",
            "thank you", "thanks", "ok bye", "okay bye",
            "धन्यवाद", "ठीक है", "बस", "नहीं", "शुक्रिया", "ओके", "बाय",
            "बस इतना ही", "थैंक यू", "थैंक्स"
        ]
        
        clean_text = re.sub(r'[,.!?।]', '', lower).strip()
        words = clean_text.split()
        
        if len(words) <= 5:
            if any(p in clean_text for p in done_phrases):
                return True
                
        return any(
            clean_text == p or clean_text.startswith(p + " ") or clean_text.endswith(" " + p)
            for p in done_phrases
        )
