"""
ScreenerAgent: discovers the caller's requirement and routes to the right specialist.
 
Routes to:
  service   — complaint, repair, installation, warranty
  sales     — product inquiry, pricing, availability
  scheduler — appointment booking, reschedule
  closer    — ticket tracking, call closure
"""
from __future__ import annotations
 
import logging
import re
from collections import deque
 
from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession
 
logger = logging.getLogger(__name__)
 
 
# Tokens that are purely confirmations, fillers, or affirmatives — no issue content.
# Devanagari matras are part of the token so we use full words, not regex \w splits.
_CONFIRMATION_WORDS = {
    # Devanagari — include both chandrabindu (ँ) and anusvara (ं) variants
    "हाँ", "हां", "हाँ", "जी", "सही", "ठीक", "है", "बिल्कुल", "बिलकुल",
    "अरे", "बाबा", "हाँजी", "हांजी", "यस", "ओके", "ओक",
    # Demonstratives / fillers that appear in confirmations like "हां यही है"
    "यही", "वही", "यह", "वह", "बस", "हम्म", "अच्छा",
    # Romanized Hindi / English
    "yes", "haan", "ji", "ok", "okay", "correct", "right",
    "done", "sure", "yep", "yeah", "fine", "sahi", "theek", "bilkul", "baba",
    "yahi", "that's", "it", "this",
}
 
# Punctuation to strip from word edges before set lookup
_STRIP_CHARS = ".,!?।॥ \t"

_SERVICE_HINTS = (
    "repair", "service", "installation", "install", "warranty", "broken",
    "not working", "not starting", "no cooling", "noise", "leak", "issue",
    "problem", "complaint", "washing machine", "ac", "air cooler", "fridge",
    "freezer", "refrigerator", "door", "drain", "power",
    "सर्विस", "मरम्मत", "दिक्कत", "समस्या", "खराब", "नहीं चल", "चालू नहीं",
    "ठंडा नहीं", "लीक", "आवाज़", "वारंटी", "इंस्टॉलेशन", "वॉशिंग मशीन",
    "मशीन", "एसी", "कूलर", "फ्रिज", "फ्रीजर", "पावर",
)
_SALES_HINTS = (
    "buy", "price", "cost", "dealer", "availability", "purchase", "new product",
    "खरीद", "कीमत", "प्राइस", "डीलर", "available", "मिल", "नया",
)
_SCHEDULER_HINTS = (
    "reschedule", "cancel appointment", "change slot", "slot change",
    "reschedule appointment", "reschedule visit", "cancel visit",
    "रीशेड्यूल", "रिशेड्यूल", "अपॉइंटमेंट बदल", "slot बदल", "visit बदल", "cancel",
)
_CLOSER_HINTS = (
    "sr", "ticket", "status", "track", "tracking", "case number",
    "service request", "complaint number",
    "स्टेटस", "टिकट", "एसआर", "केस नंबर", "ट्रैक",
)
 
 
def _is_confirmation_only(text: str) -> bool:
    """
    Return True if the transcript contains only confirmation/filler words
    and no issue-related content.
 
    Uses space-split (not regex \\w+) to preserve Devanagari matras.
 
    Examples that return True:  'हां सही है', 'अरे बाबा सही है', 'yes', 'okay ji'
    Examples that return False: 'हाँ मेरी AC खराब है', 'yes washing machine leak'
    """
    tokens = [t.strip(_STRIP_CHARS).lower() for t in text.split()]
    tokens = [t for t in tokens if t]  # drop empties
    if not tokens:
        return False
    # Short transcript (≤6 tokens) where every token is a known confirmation word
    return len(tokens) <= 6 and all(t in _CONFIRMATION_WORDS for t in tokens)


def _route_from_issue(issue: str) -> str | None:
    lower = issue.lower()
    if any(hint in lower for hint in _SCHEDULER_HINTS):
        return "scheduler"
    if any(hint in lower for hint in _CLOSER_HINTS):
        return "closer"
    if any(hint in lower for hint in _SALES_HINTS):
        return "sales"
    if any(hint in lower for hint in _SERVICE_HINTS):
        return "service"
    return None
 
 
def _message_similarity(msg1: str, msg2: str) -> float:
    """Compute similarity between two messages (0.0 to 1.0) using word-set intersection."""
    words1 = set(msg1.lower().split())
    words2 = set(msg2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    return intersection / union
 
 
class ScreenerAgent(BaseAgent):
    name = "screener"
    can_handoff_to = ["service", "sales", "scheduler", "closer"]
 
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_responses = deque(maxlen=5)  # Track last 5 responses for loop detection
 
    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        # Outbound calls: skip all inbound intent-routing and context-injection logic
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            reply = await self._chat(session, transcript)
            handoff = self._parse_handoff(reply)
            end_call = "[END_CALL]" in reply
            clean_reply = re.sub(
                r"\[HANDOFF:[^\]]+\]|\[END_CALL\]|\[LANG:[^\]]+\]",
                "", reply,
            ).strip()
            # Screener must speak its intro and wait for the customer's "shall we begin?"
            # confirmation before cascading to service. On the first screener turn the
            # transcript is the hello-stage "हाँ" — not a screener readiness confirmation.
            # Suppress the service handoff on that first turn so the customer hears the
            # screener intro and gets a chance to respond before Q1 starts.
            if handoff == "service" and not session.get("screener_intro_done") and clean_reply:
                session.set("screener_intro_done", True)
                return AgentResponse(text=clean_reply)
            if handoff:
                return AgentResponse(text=clean_reply, handoff=HandoffSignal(target=handoff))
            if end_call:
                return AgentResponse(text=clean_reply, end_call=True)
            return AgentResponse(text=clean_reply)

        customer_name   = session.get("name", "")
        customer_mobile = session.get("mobile", "")
        customer_intent = session.get("intent", "") or session.get("reported_issue", "")
        language        = session.get("language", session.current_language)
 
        # Force language lock
        if language:
            session.set_language(language)
            # Refresh language_instruction in session metadata so _build_messages
            # injects the correct LANGUAGE LOCK — set_language() only updates the
            # tracker, not the metadata key that _build_messages reads.
            from core.streaming_pipeline import _LANG_INSTRUCTIONS
            session.set("language_instruction", _LANG_INSTRUCTIONS.get(language, ""))
 
        # ── Fresh-start on handoff confirmation transcripts ────────────────
        # When hello hands off, the orchestrator passes the same confirmation
        # transcript ("हां सही है", "yes", etc.) to screener. That phrase is
        # meaningless for issue detection and causes the LLM to output empty.
        # If no intent is known yet and the transcript is a pure confirmation,
        # skip the LLM and ask the question directly.
        # if customer_intent and _is_confirmation_only(transcript):
        #     route = _route_from_issue(customer_intent)
        #     if route:
        #         logger.info(
        #         f"[SCREENER DIRECT ROUTE] intent='{customer_intent}' → {route} "
        #         f"(skipping LLM, confirmation-only transcript)"
        #     )
        #         session.add_message("user", transcript)
        #         session.add_message("assistant", f"[HANDOFF:{route}]")
        #         return AgentResponse(text="", handoff=HandoffSignal(target=route))
            
        if customer_intent and _is_confirmation_only(transcript):
            route = self._route_from_intent(customer_intent)
            logger.info(
                f"[SCREENER DIRECT ROUTE] intent='{customer_intent}' → {route} "
                f"(skipping LLM, confirmation-only transcript)"
            )
            return AgentResponse(
                text="",
                handoff=HandoffSignal(target=route),
            )

        if not customer_intent and _is_confirmation_only(transcript):
            if customer_name:
                ask = (
                    f"जी {customer_name} जी, आपको किस चीज़ में सहायता चाहिए?"
                    if language == "hi"
                    else f"Sure {customer_name}, how can I help you today?"
                )
            else:
                ask = (
                    "जी, आपको किस चीज़ में सहायता चाहिए?"
                    if language == "hi"
                    else "How can I help you today?"
                )
            logger.info(f"[SCREENER FRESH START] Skipping LLM for confirmation-only transcript: '{transcript[:40]}'")
            session.add_message("user", transcript)
            session.add_message("assistant", ask)
            return AgentResponse(text=ask)
 
        context_prefix = self._build_context(customer_name, customer_mobile, language, customer_intent)
        enriched_transcript = f"{context_prefix}\nCustomer said: {transcript}"
 
        reply = await self._chat(session, enriched_transcript)
               # ── Guard: LLM returned empty — ask the question instead of going silent ──
        if not reply or not reply.strip():
            logger.warning(f"[SCREENER] LLM returned empty for transcript: '{transcript[:60]}'")
            if customer_intent:
                # Intent known but LLM couldn't decide — route to service
                return AgentResponse(
                    text="",
                    handoff=HandoffSignal(target=self._route_from_intent(customer_intent)),
                )
            ask = (
                f"जी {customer_name} जी, आपको किस चीज़ में सहायता चाहिए?"
                if language == "hi"
                else f"Sure {customer_name}, how can I help you today?"
            ) if customer_name else (
                "जी, आपको किस चीज़ में सहायता चाहिए?"
                if language == "hi"
                else "How can I help you today?"
            )
            session.add_message("user", transcript)
            session.add_message("assistant", ask)
            return AgentResponse(text=ask)
 
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply
 
        # Strip ALL control tags including [LANG:x]
        clean_reply = re.sub(
            r"\[HANDOFF:[^\]]+\]|\[END_CALL\]|\[LANG:[^\]]+\]",
            "", reply
        ).strip()
 
        # ── Loop detection: if same response repeated 2+ times, force handoff to service ──
        if clean_reply and not handoff and not end_call:
            self._last_responses.append(clean_reply)
            
            # Check for loops: any 2 consecutive responses with >50% similarity = loop
            if len(self._last_responses) >= 2:
                last_reply = self._last_responses[-1]
                prev_reply = self._last_responses[-2]
                similarity = _message_similarity(last_reply, prev_reply)
                
                if similarity > 0.50:  # Loop detected
                    logger.warning(
                        f"[SCREENER LOOP DETECTED] Similarity={similarity:.2f} (threshold=0.50). "
                        f"Last: '{last_reply[:60]}...' | Prev: '{prev_reply[:60]}...' → "
                        f"Force handoff to service"
                    )
                    # Force handoff to service to complete booking
                    return AgentResponse(
                        text="",
                        handoff=HandoffSignal(target="service"),
                    )
            else:
                logger.debug(f"[SCREENER] Response tracked (total: {len(self._last_responses)})")
 
        if handoff:
            # Silent handoff — suppress any LLM-generated transition phrase
            # ("transferring you to service team", "let me connect you", etc.).
            # The destination agent immediately greets the customer; no bridge text needed.
            return AgentResponse(
                text="",
                handoff=HandoffSignal(target=handoff),
            )
        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)
        return AgentResponse(text=clean_reply)
 
    async def stream_handle(self, transcript: str, session: CallSession):
        """
        Override the base streaming path.
 
        Screener responses are always one sentence, so streaming buys nothing.
        More importantly, base stream_handle() yields TTS sentences as they
        arrive from the LLM — the LLM often inserts a transition phrase
        ("I'll transfer you…") before [HANDOFF:xxx], which is already spoken
        by the time the handoff tag is detected.
 
        By going through handle() first (full reply in hand), we can suppress
        that phrase (handle() forces text="" on any handoff) before any audio
        is queued.
        """
        resp = await self.handle(transcript, session)
        clean_text = (resp.text or "").strip()
        if clean_text:
            for part in re.split(r'(?<=[.!?])\s+', clean_text):
                part = part.strip()
                if part:
                    yield part, None
        yield None, resp
 
    def _build_context(self, name: str, mobile: str, language: str, intent: str = "") -> str:
        lang_instruction = (
            "Reply in Hindi only. Tag every reply [LANG:hi]."
            if language == "hi"
            else "Reply in English only. Tag every reply [LANG:en]."
        )
        lines = [
            "[CONTEXT — do NOT ask again]",
            f"Customer name: {name}" if name else "",
            f"Customer mobile: {mobile}" if mobile else "",
            f"Language locked: {language}",
            lang_instruction,
        ]
        if intent:
            lines.append(
                f"Customer already stated their issue: \"{intent}\" — "
                f"DO NOT ask 'How can I help you?' again. Route immediately based on this issue."
            )
        lines.append("[END CONTEXT]")
        return "\n".join(l for l in lines if l)
    
    @staticmethod
    def _route_from_intent(intent: str) -> str:
        """Map a customer intent to the right agent. Defaults to service."""
        lower = intent.lower()
        _SALES_KEYWORDS = [
            "buy", "price", "cost", "new", "purchase", "kharidna", "khareedna",
            "rate", "offer", "discount", "compare", "model", "available",
            "खरीदना", "कीमत", "दाम", "नया", "ऑफर",
        ]
        _SCHEDULER_KEYWORDS = [
            "reschedule", "cancel appointment", "booking", "slot",
            "रीशेड्यूल", "बुकिंग",
        ]
        if any(kw in lower for kw in _SALES_KEYWORDS):
            return "sales"
        if any(kw in lower for kw in _SCHEDULER_KEYWORDS):
            return "scheduler"
        # Default: service (complaint, repair, installation, warranty — most common)
        return "service"
 
    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
 
 
