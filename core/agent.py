"""
Base agent abstraction. Every specialized agent extends BaseAgent.
 
Flow:
  user speech -> STT -> agent.handle(transcript, session) -> TTS -> audio out
  agent.handle() returns AgentResponse which may include a HandoffSignal.
 
Optional capabilities (set via constructor):
  rag          - a BaseRetriever; retrieves context before every LLM call
  mcp_registry - an MCPRegistry; allows LLM to call external HTTP tools
"""
from __future__ import annotations
 
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator
 
import pytz

if TYPE_CHECKING:
    from providers.rag.base import BaseRetriever
    from providers.mcp.registry import MCPRegistry
 
logger = logging.getLogger(__name__)
 
# Token format the LLM uses to invoke an MCP tool:  [MCP:tool_name:{"arg":"val"}]
_MCP_PATTERN = re.compile(r"\[MCP:(\w+):(\{.*?\})\]", re.DOTALL)
 
# Control-marker cleanup — strips before yielding sentences to TTS
_REPLY_CLEAN_RE = re.compile(
    r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP|TOOL):[^\]]*\]|\[END_CALL\]"
)
_HANDOFF_RE     = re.compile(r"\[HANDOFF:(\w+)\]")
# Sentence boundary: whitespace immediately after .  !  ?
_SENT_BOUNDARY_RE = re.compile(r'(?<=[.!?])\s')
_HOLLOW_EMPATHY_PATTERNS = [
    re.compile(r"\b(?:main|mai|mein)\s+samajhti\s+(?:hoon|hu|hun)\b[\s,.\-–—]*", re.IGNORECASE),
    re.compile(r"\b(?:main|mai|mein)\s+samajh\s+sakti\s+(?:hoon|hu|hun)\b[\s,.\-–—]*", re.IGNORECASE),
    re.compile(r"\b(?:main|mai|mein)\s+samajh\s+rahi\s+(?:hoon|hu|hun)\b[\s,.\-–—]*", re.IGNORECASE),
    re.compile(r"मैं\s+समझती\s+हूँ[\s,।\-–—]*"),
    re.compile(r"मैं\s+समझ\s+सकती\s+हूँ[\s,।\-–—]*"),
    re.compile(r"मैं\s+समझ\s+रही\s+हूँ[\s,।\-–—]*"),
]
 
_LANG_NAME = {
    "hi": "Hindi (हिंदी)",
    "bn": "Bengali (বাংলা)",
    "te": "Telugu (తెలుగు)",
    "mr": "Marathi (मराठी)",
    "ta": "Tamil (தமிழ்)",
    "gu": "Gujarati (ગુજરાતી)",
    "kn": "Kannada (ಕನ್ನಡ)",
    "pa": "Punjabi (ਪੰਜਾਬੀ)",
    "ml": "Malayalam (മലയാളം)",
    "or": "Odia (ଓଡ଼ିଆ)",
    "en": "English",
}
 
# ── Assistant prefill starters ───────────────────────────────────────────────
_LANG_PREFILL: dict[str, str] = {
    "hi": "जी,",
    "bn": "হ্যাঁ,",
    "te": "అవును,",
    "mr": "हो,",
    "ta": "ஆம்,",
    "gu": "હા,",
    "kn": "ಹೌದು,",
    "pa": "ਹਾਂ,",
    "ml": "ശരി,",
    "or": "ହଁ,",
}

# ── Token estimation ─────────────────────────────────────────────────────────
_HISTORY_TOKEN_BUDGET = 1500

def _estimate_tokens(text: str) -> int:
    """Fast token estimate: count ASCII vs non-ASCII to handle multilingual text."""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii   = len(text) - ascii_chars
    return (ascii_chars // 4) + (non_ascii // 2) + 1


def _sanitize_hollow_empathy(text: str) -> str:
    """Remove hollow empathy phrases from the generated response."""
    if not text:
        return text
    for pattern in _HOLLOW_EMPATHY_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()

def _detect_user_emotion(text: str) -> str:
    lowered = text.lower()
    for label, keywords in _EMOTION_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return label
    return "neutral"

# ── Service agent loop detection ─────────────────────────────────────────────
# Phrases that signal the service agent is looping post-SR.
# If ANY of these appear in a reply AND the same phrase already appeared
# in a previous assistant turn → the agent is stuck → force handoff:closer.
_SERVICE_LOOP_PHRASES = [
    "आपकी समस्या का समाधान जल्द ही हो जाएगा",
    "हमारे इंजीनियर आपको फोन करेंगे",
    "इंजीनियर जल्द ही संपर्क करेंगे",
    "इंजीनियर आपसे संपर्क करेंगे",
    "engineer will contact you",
    "engineer will call you",
    "your issue will be resolved",
]

# Short acknowledgements that mean "I heard you, keep going / we're done"
_ACK_PHRASES = [
    "ठीक है", "अच्छा", "हाँ", "हां", "जी", "समझ गई",
    "okay", "ok", "theek", "noted", "yes", "sure",
]
_EMOTION_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("distressed", ("sad", "upset", "worried", "scared", "crying", "depressed", "dukhi", "pareshan", "tension", "दुख", "परेशान", "टेंशन", "रो", "घबर")),
    ("frustrated", ("angry", "frustrated", "annoyed", "irritated", "complaint", "worst", "bad service", "bekaar", "gussa", "faltu", "bakwaas", "नाराज़", "गुस्सा", "बेकार", "खराब service")),
    ("happy", ("happy", "great", "awesome", "perfect", "good", "excellent", "excited", "thanks a lot", "thank you so much", "bahut achha", "badiya", "mast", "खुश", "बहुत अच्छा", "बढ़िया")),
    ("confused", ("confused", "don't understand", "dont understand", "not understand", "what do you mean", "kaise", "samajh nahi", "samajh nahi aa", "clear nahi", "confuse", "समझ नहीं", "कैसे", "क्या मतलब")),
]

_EMOTION_GUIDANCE = {
    "distressed": (
        "CURRENT CALLER EMOTION: sad or distressed.\n"
        "Respond gently and steadily. Start with one brief human reassurance, "
        "then move to the next concrete action. Avoid sounding overly cheerful."
    ),
    "frustrated": (
        "CURRENT CALLER EMOTION: frustrated or angry.\n"
        "Stay calm, grounded, and respectful. Do not be defensive. Acknowledge "
        "the frustration once, then move directly into a fix or the next clear question."
    ),
    "happy": (
        "CURRENT CALLER EMOTION: happy or excited.\n"
        "Match with light positive energy, but stay concise and useful. Keep the tone warm, confident, and quick."
    ),
    "confused": (
        "CURRENT CALLER EMOTION: confused or uncertain.\n"
        "Use simpler words, short sentences, and only one question at a time. Explain the next step plainly."
    ),
    "neutral": (
        "CURRENT CALLER EMOTION: neutral.\n"
        "Keep the tone warm, efficient, and natural. Do not overdo empathy."
    ),
}

# Hard token ceiling for the service agent.
# If we're already at this many tokens the call has run very long — handoff.
_SERVICE_TOKEN_HARD_LIMIT = 13_000


# ── IST timezone ─────────────────────────────────────────────────────────────
_IST = pytz.timezone("Asia/Kolkata")

_HINDI_DAYS = {
    "Monday":    "Somwar (सोमवार)",
    "Tuesday":   "Mangalwar (मंगलवार)",
    "Wednesday": "Budhwar (बुधवार)",
    "Thursday":  "Guruwar (गुरुवार)",
    "Friday":    "Shukrawar (शुक्रवार)",
    "Saturday":  "Shaniwar (शनिवार)",
    "Sunday":    "Raviwar (रविवार)",
}


def _build_date_context() -> str:
    """
    Returns a compact date-context block to prepend to every system prompt.
    Called fresh on each LLM turn so the date is always accurate.
    """
    now       = datetime.now(_IST)
    yesterday = now - timedelta(days=1)
    kal       = now + timedelta(days=1)
    parso     = now + timedelta(days=2)
    narso     = now + timedelta(days=3)

    def format_day(dt):
        en_day = dt.strftime("%A")
        return _HINDI_DAYS.get(en_day, en_day)

    fmt = "%d %B %Y"   # e.g. 10 April 2026

    return (
        f"## Current Date & Time (IST)\n"
        f"- Aaj (today)  : {now.strftime(fmt)} — {format_day(now)}\n"
        f"- Kal (tomorrow): {kal.strftime(fmt)} — {format_day(kal)}\n"
        f"- Parso (day after tomorrow): {parso.strftime(fmt)} — {format_day(parso)}\n"
        f"- Narso (3 days from now): {narso.strftime(fmt)} — {format_day(narso)}\n"
        f"- Current time : {now.strftime('%I:%M %p')} IST\n\n"
        f"When a caller uses relative time words (aaj, kal, parso, pichle somwar, "
        f"is hafte, pichle mahine, etc.) ALWAYS resolve them to the actual calendar "
        f"date shown above before using or confirming the date with the customer.\n"
    )


def _service_loop_detected(reply: str, session) -> bool:
    """
    Returns True when the service agent is repeating a post-booking reassurance
    phrase it has already said in this call.

    Two conditions must BOTH be true:
      1. The current reply contains one of the known loop phrases.
      2. The identical (or very similar) phrase already appears in a previous
         assistant turn in session history.

    This is intentionally narrow — it only fires on the specific phrases that
    caused the observed loops, so it cannot accidentally suppress legitimate
    first-time uses of those phrases.
    """
    if session.current_agent != "service":
        return False

    # Check if this reply contains a known loop phrase
    matched_phrase = None
    for phrase in _SERVICE_LOOP_PHRASES:
        if phrase in reply:
            matched_phrase = phrase
            break

    if matched_phrase is None:
        return False  # no loop phrase in current reply → not a loop

    # Check if the same phrase appeared in any PREVIOUS assistant turn
    history = session.history  # list of {"role": ..., "content": ...}
    assistant_turns = [
        m["content"] for m in history
        if m["role"] == "assistant"
    ]

    # Exclude the very last turn (which IS the current reply if already added)
    # We compare against turns BEFORE this one
    prior_turns = assistant_turns[:-1] if assistant_turns else []

    for past in prior_turns:
        if matched_phrase in past:
            logger.warning(
                "[SERVICE LOOP DETECTED] Phrase already said in a prior turn: "
                "'%s...' → force handoff:closer",
                matched_phrase[:60],
            )
            return True

    return False


def _service_token_limit_exceeded(messages: list[dict]) -> bool:
    """
    Returns True if estimated input token count exceeds the hard ceiling.
    A very long call where the service agent keeps running = something is wrong.
    Force handoff to closer immediately.
    """
    total = sum(_estimate_tokens(m["content"]) for m in messages)
    if total > _SERVICE_TOKEN_HARD_LIMIT:
        logger.warning(
            "[SERVICE TOKEN LIMIT] ~%d tokens > %d ceiling → force handoff:closer",
            total, _SERVICE_TOKEN_HARD_LIMIT,
        )
        return True
    return False


@dataclass
class HandoffSignal:
    """Returned when an agent wants to transfer control to another agent."""
    target:  str
    message: str = ""
    data:    dict[str, Any] = field(default_factory=dict)
 
 
@dataclass
class AgentResponse:
    text:     str
    handoff:  HandoffSignal | None = None
    end_call: bool = False
 
 
class BaseAgent(ABC):
    name: str
    can_handoff_to: list[str] = []
 
    def __init__(
        self,
        llm,
        prompt:       str,
        persona:      str = "",
        rag:          "BaseRetriever | None" = None,
        mcp_registry: "MCPRegistry | None"   = None,
        rag_top_k:    int = 3,
    ):
        self.llm          = llm
        self.system_prompt = f"{persona.strip()}\n\n{prompt}".strip() if persona.strip() else prompt
        self.rag           = rag
        self.mcp_registry  = mcp_registry
        self.rag_top_k     = rag_top_k
 
    @abstractmethod
    async def handle(self, transcript: str, session) -> AgentResponse:
        ...
 
    # ── RAG ──────────────────────────────────────────────────────────────────
 
    def _needs_rag(self, query: str) -> bool:
        """Skip RAG for very short or trivial utterances (saves ~300 tokens + latency)."""
        stripped = query.strip().lower()
        if len(stripped) < 8:
            return False
        trivial = {
            "yes", "no", "ok", "okay", "hello", "hi", "hey", "haan", "nahi",
            "ji", "ha", "hmm", "thanks", "thank you", "bye", "haa", "naa",
            "theek hai", "accha", "shukriya",
        }
        return stripped not in trivial

    async def _retrieve_context(self, query: str) -> str:
        if not self.rag:
            return ""
        if not self._needs_rag(query):
            logger.debug(f"[{self.name}] RAG skipped for trivial query: '{query[:30]}'")
            return ""
        chunks = await self.rag.retrieve(query, top_k=self.rag_top_k)
        if not chunks:
            return ""
        context = self.rag.format_context(chunks)
        logger.debug(f"[{self.name}] RAG retrieved {len(chunks)} chunks for query: {query[:60]}")
        return context
 
    # ── MCP ──────────────────────────────────────────────────────────────────
 
    async def _execute_mcp_calls(self, text: str, session) -> tuple[str, dict[str, Any]]:
        if not self.mcp_registry:
            return text, {}
 
        matches = _MCP_PATTERN.findall(text)
        if not matches:
            return text, {}
 
        results: dict[str, Any] = {}
        for tool_name, args_json in matches:
            try:
                args = json.loads(args_json)
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] Invalid MCP args JSON for tool '{tool_name}'")
                continue
 
            logger.info(f"[{self.name}] MCP call: {tool_name}({args})")
            result = await self.mcp_registry.call(tool_name, args)
 
            if result.success:
                results[tool_name] = result.data
                session.set(f"mcp_{tool_name}_result", result.data)
                logger.info(f"[{self.name}] MCP result: {tool_name} -> {str(result.data)[:100]}")
            else:
                logger.error(f"[{self.name}] MCP error: {tool_name} -> {result.error}")
                results[tool_name] = f"Error: {result.error}"
 
        return text, results
 
    # ── LLM helpers ──────────────────────────────────────────────────────────
 
    def _log_token_estimate(self, messages: list[dict]) -> None:
        """Log estimated token count for monitoring. Helps catch runaway usage."""
        total = sum(_estimate_tokens(m["content"]) for m in messages)
        logger.info(f"[{self.name}] LLM call ~{total} input tokens ({len(messages)} messages)")
        if total > 4000:
            logger.warning(f"[{self.name}] HIGH TOKEN COUNT: ~{total} tokens — check history/prompt size")

    def _build_messages(self, session, user_message: str, rag_context: str) -> list[dict]:
        """
        Build the full messages list (system + history + user) for an LLM call.
        Pure helper — no side effects, no I/O. Shared by _chat() and stream_handle().
        """
        # ── Inject current IST date so agent resolves relative terms correctly ──
        system_content = _build_date_context() + "\n" + self.system_prompt
 
        if rag_context:
            system_content += (
                "\n\n## Relevant Knowledge Base Context\n"
                "Use the following retrieved information to inform your response. "
                "Do not quote sources verbatim; use the information naturally.\n\n"
                + rag_context
            )

        if self.mcp_registry and self.mcp_registry.list_tools():
            system_content += "\n\n" + self.mcp_registry.tool_descriptions_for_prompt()
        
        emotion = _detect_user_emotion(user_message)
        # Outbound NPS survey calls follow a structured script — the inbound
        # "1 to 3 short sentences" rule conflicts with question + rating
        # readback + reason-capture flow, and the emotion override competes
        # with the survey's prescribed empathy phrasing.
        is_outbound = (
            session.get("direction") == "outbound"
            or session.get("support_domain") == "outbound"
        )
        if is_outbound:
            # Inject the lead's CRM context so the LLM knows the customer's name,
            # product/order details etc. without ever asking for them.
            lead_fields = [
                ("customer_name",      "Customer name"),
                ("phone_number",       "Phone number"),
                ("customer_type",      "Customer type"),
                ("product_category",   "Product category"),
                ("product_name",       "Product / material"),
                ("purchase_date",      "Order / purchase date"),
                ("address",            "City / address"),
                ("pincode",            "Pincode"),
                ("preferred_language", "Preferred language"),
            ]
            lead_lines = [
                f"- {label}: {val}"
                for key, label in lead_fields
                if (val := str(session.get(key, "") or "").strip())
            ]
            if lead_lines:
                system_content += (
                    "\n\n## Known Customer Context (from CRM — already verified)\n"
                    "These details are already on file. Use them naturally in conversation. "
                    "NEVER ask the customer for any of these — they are pre-filled from the lead record:\n"
                    + "\n".join(lead_lines)
                    + "\n\nExamples of natural usage:\n"
                    "- Address them by name (e.g., \"Rajesh जी\") instead of asking who you're speaking to.\n"
                    "- Reference their product category or recent order if relevant to the feedback.\n"
                    "- Mention order/account status only if directly relevant to a complaint.\n"
                    "- Only re-confirm a detail if the customer explicitly says it's wrong."
                )
            system_content += (
                "\n\n## Voice Response Style (Outbound Survey)\n"
                "- Speak the script in the system prompt above as written. Do not paraphrase survey questions.\n"
                "- Ask exactly one survey question at a time and wait for the customer's reply.\n"
                "- Always confirm numerical ratings back in words (e.g., 'seven') before moving on.\n"
                "- For low ratings, ALWAYS ask the follow-up reason question before the next question.\n"
                "- Emit [HANDOFF:xxx] or [END_CALL] markers exactly when the prompt's Handoff Rule says to.\n"
            )
        else:
            system_content += (
                "\n\n## Voice Response Style\n"
                "- Voice calls need low latency: keep replies compact.\n"
                "- Prefer 1 to 3 short sentences.\n"
                "- Ask only one main question at a time unless confirming numbers, dates, or slots.\n"
                "- Do not repeat the same reassurance phrase across turns.\n\n"
                "## Emotion Adaptation\n"
                f"{_EMOTION_GUIDANCE[emotion]}"
            )

        lang_code = session.get("language", "hi")  # Deepgram STT language
        lang_name = _LANG_NAME.get(lang_code, "English")

        # Detect the language of the current caller turn so we can give the
        # LLM an accurate mirror hint — the session lang_code only switches on
        # explicit requests (for Deepgram), so it lags behind the actual turn.
        from providers.language.detector import detect_language as _detect_turn_lang
        turn_lang      = _detect_turn_lang(user_message)
        turn_lang_name = _LANG_NAME.get(turn_lang, "English")

        if lang_code == "hi":
            # When the caller's turn contains Devanagari script the language is
            # unambiguous — enforce a hard lock so the LLM cannot drift to English.
            # Only fall back to the soft mirror for Roman-script turns (Hinglish /
            # English) where the intent is genuinely ambiguous.
            from providers.language.detector import is_script_based as _is_script_based
            if _is_script_based(user_message):
                system_content += (
                    f"\n\nLANGUAGE RULE (HARD — NO EXCEPTIONS):\n"
                    f"The caller just spoke in Hindi (Devanagari script detected). "
                    f"You MUST respond entirely in Hindi. "
                    f"Do not write a single English sentence or switch to English. "
                    f"English words the caller used (like 'okay', 'yes') are Hinglish — "
                    f"they do NOT indicate an English preference."
                )
            else:
                # Roman-script turn: may be Hinglish or English — use soft mirror.
                system_content += (
                    f"\n\nLANGUAGE RULE (mirror the caller):\n"
                    f"The caller's last message was in {turn_lang_name}. "
                    f"Mirror their language naturally:\n"
                    f"- If they spoke Hindi → respond in Hindi.\n"
                    f"- If they spoke English → respond in English.\n"
                    f"- If they mixed Hindi and English (Hinglish) → match their mix.\n"
                    f"- A single word like 'yes', 'okay', 'hello' by a Hindi speaker "
                    f"does NOT mean they switched to English — stay in Hindi unless "
                    f"their full sentence is in English."
                )
        elif lang_code not in ("en", ""):
            # Regional language (Tamil, Telugu, Bengali, etc.): callers
            # occasionally use English technical terms but the base language
            # is clear. Keep a firm but not extreme instruction.
            system_content += (
                f"\n\nLANGUAGE RULE: The caller uses {lang_name}. "
                f"Respond primarily in {lang_name}. "
                f"English technical terms the caller uses are fine to echo back, "
                f"but keep your response in {lang_name} overall."
            )
 
        clean_history = [
            msg for msg in session.history
            if not (
                msg["content"].startswith("[SYSTEM:") or
                msg["content"].startswith("[LANGUAGE INSTRUCTION]") or
                msg["content"].startswith("[LANG_SWITCH:") or
                msg["content"].startswith("[Switching to ") or
                msg["content"].startswith("[Understood. Switching") or
                msg["content"].startswith("[Understood. I will")
            )
        ]

        recent: list[dict] = []
        token_count = 0
        for msg in reversed(clean_history):
            msg_tokens = _estimate_tokens(msg["content"])
            if token_count + msg_tokens > _HISTORY_TOKEN_BUDGET and recent:
                break
            recent.append(msg)
            token_count += msg_tokens
        recent.reverse()
 
        history_messages = []
        prev_lang = None
        for msg in recent:
            msg_lang = msg.get("lang", lang_code)
            if prev_lang is not None and msg_lang != prev_lang:
                switch_name = _LANG_NAME.get(msg_lang, msg_lang)
                history_messages.append({
                    "role":    "assistant",
                    "content": f"[Switching to {switch_name} as the user requested.]"
                })
            history_messages.append({"role": msg["role"], "content": msg["content"]})
            prev_lang = msg_lang
 
        messages = [{"role": "system", "content": system_content}]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": user_message})

        # For non-Hindi Indian scripts, add an assistant prefill starter so the
        # LLM begins in the correct character set (Tamil, Telugu, Bengali etc.).
        # Skipped for Hindi because Hinglish mixing is intentional and the soft
        # mirror rule above handles it. Skipped for English (unnecessary).
        if lang_code not in ("hi", "en", "") and lang_code in _LANG_PREFILL:
            messages.append({
                "role":    "assistant",
                "content": _LANG_PREFILL[lang_code]
            })

        return messages
 
    def _preprocess_transcript(self, transcript: str, session) -> str:
        """
        Hook for subclasses to transform the transcript before it reaches the LLM.
        Default: no-op. Override (e.g. in HelloAgent) to inject structured data.
        """
        return transcript
 
    async def _chat(self, session, user_message: str) -> str:
        """
        Full pipeline:
          1. (Optional) Preprocess transcript (subclass hook)
          2. (Optional) Retrieve RAG context for the user message
          3. Build messages (system + history + user)
          4. ── Service loop / token guard (pre-LLM) ──
          5. Call LLM
          6. ── Service loop detection (post-LLM) ──
          7. (Optional) Execute any MCP tool calls in the response
          8. If MCP tools were called, do a follow-up LLM turn with results
          9. Update session history and return final reply
        """
        user_message = self._preprocess_transcript(user_message, session)
        rag_context  = await self._retrieve_context(user_message)
        messages     = self._build_messages(session, user_message, rag_context)
        self._log_token_estimate(messages)

        # ── Pre-LLM guard: token ceiling ─────────────────────────────────────
        # If the service agent call has grown excessively long, something is
        # wrong. Force handoff before spending more tokens.
        if session.current_agent == "service" and _service_token_limit_exceeded(messages):
            return "[HANDOFF:closer]"

        # ── Optional LLM cache (stateless agents only) ───────────────────────
        # Cache.get_llm internally enforces both LLM_CACHE_ENABLED and the
        # stateful-agent denylist (service/sales/scheduler/screener/closer never
        # cached). Hello agent benefits most: greetings + mobile-confirm
        # acknowledgements are nearly identical across calls.
        cached_reply = None
        cache = None
        try:
            from core.response_cache import get_cache
            cache = get_cache()
            if cache is not None:
                cached_reply = await cache.get_llm(
                    self.name, session.current_language or "en", user_message
                )
        except Exception:
            cached_reply = None
        if cached_reply is not None:
            session.add_message("user", user_message)
            session.add_message("assistant", cached_reply)
            return cached_reply

        reply = await self.llm.chat(messages)
        reply = _sanitize_hollow_empathy(reply)

        # ── Post-LLM guard: loop phrase detection ─────────────────────────────
        # Check BEFORE adding to history so prior_turns comparison is clean.
        if _service_loop_detected(reply, session):
            return "[HANDOFF:closer]"

        reply, mcp_results = await self._execute_mcp_calls(reply, session)

        if mcp_results:
            tool_summary = "\n".join(
                f"Result of {name}: {result}" for name, result in mcp_results.items()
            )
            follow_up_messages = [
                messages[0],
                {"role": "user",      "content": user_message},
                {"role": "assistant", "content": reply},
                {"role": "user",      "content": f"[TOOL_RESULTS]\n{tool_summary}\n\nNow respond to the customer based on these results. Be concise."},
            ]
            reply = await self.llm.chat(follow_up_messages)
            reply = _sanitize_hollow_empathy(reply)

        # Store in cache only when no MCP tool was used and the reply has no
        # session-specific control tags (handoffs / extracted entities). Those
        # depend on call state and are not safe to replay.
        try:
            if (
                cache is not None
                and not mcp_results
                and "[HANDOFF:" not in reply
                and "[MOBILE:" not in reply
                and "[NAME:" not in reply
                and "[INTENT:" not in reply
                and "[END_CALL]" not in reply
            ):
                await cache.set_llm(
                    self.name, session.current_language or "en", user_message, reply
                )
        except Exception:
            pass

        session.add_message("user",      user_message)
        session.add_message("assistant", reply)

        return reply
 
    async def stream_handle(self, transcript: str, session):
        """
        Streaming alternative to handle().
 
        Yields (sentence, None) for each TTS-ready sentence as tokens arrive
        from the LLM, then yields (None, AgentResponse) once complete.

        Cuts time-to-first-audio by ~50-70%: TTS starts on the first sentence
        while the LLM is still generating the rest of the reply.

        MCP tool calls (if any) are processed after streaming finishes and their
        follow-up reply is yielded as sentences before the final AgentResponse.
        """
        transcript  = self._preprocess_transcript(transcript, session)
        rag_context = await self._retrieve_context(transcript)
        messages    = self._build_messages(session, transcript, rag_context)
        self._log_token_estimate(messages)

        # ── Pre-LLM guard: token ceiling (streaming path) ─────────────────────
        if session.current_agent == "service" and _service_token_limit_exceeded(messages):
            logger.warning("[SERVICE TOKEN LIMIT] Streaming path → force handoff:closer")
            yield None, AgentResponse(
                text="",
                handoff=HandoffSignal(target="closer"),
            )
            return

        full_reply = ""
        buf        = ""
        # Reset any partial from a previous (cancelled) turn so the barge-in
        # recovery path in streaming_pipeline._handle_turn doesn't pick up
        # stale text from before this turn started.
        session.set("_partial_assistant", "")

        async for token in self.llm.stream_chat(messages):
            full_reply += token
            buf        += token
            # Track running partial so a barge-in cancellation can persist
            # what the agent had generated up to that point (otherwise the
            # turn is dropped from history and the LLM repeats itself next turn).
            session.set("_partial_assistant", full_reply)

            while True:
                m = _SENT_BOUNDARY_RE.search(buf)
                if m:
                    sentence = buf[:m.start()]
                    buf      = buf[m.end():]
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

        # Flush remaining buffer
        if buf.strip():
            clean = _REPLY_CLEAN_RE.sub("", buf).strip()
            if clean:
                yield clean, None

        # ── Post-LLM guard: loop phrase detection (streaming path) ────────────
        # Check BEFORE adding to history.
        if _service_loop_detected(full_reply, session):
            logger.warning("[SERVICE LOOP DETECTED] Streaming path → force handoff:closer")
            session.add_message("user",      transcript)
            session.add_message("assistant", full_reply)
            session.set("_partial_assistant", "")
            yield None, AgentResponse(
                text="",
                handoff=HandoffSignal(target="closer"),
            )
            return

        # Execute MCP tool calls on the completed reply
        full_reply, mcp_results = await self._execute_mcp_calls(full_reply, session)
 
        if mcp_results:
            tool_summary = "\n".join(
                f"Result of {name}: {result}" for name, result in mcp_results.items()
            )
            follow_up_messages = [
                messages[0],
                {"role": "user",      "content": transcript},
                {"role": "assistant", "content": full_reply},
                {"role": "user",      "content": f"[TOOL_RESULTS]\n{tool_summary}\n\nNow respond to the customer based on these results. Be concise."},
            ]
            full_reply   = await self.llm.chat(follow_up_messages)
            full_reply   = _sanitize_hollow_empathy(full_reply)
            clean_follow = _REPLY_CLEAN_RE.sub("", full_reply).strip()
            for part in re.split(r'(?<=[.!?])\s+', clean_follow):
                part = part.strip()
                if part:
                    yield part, None
 
        session.add_message("user",      transcript)
        session.add_message("assistant", full_reply)
        # Successful completion: clear partial so the cancellation-recovery path
        # in _handle_turn doesn't double-save this same turn on a future cancel.
        session.set("_partial_assistant", "")

        for tag in ("NAME", "MOBILE"):
            m = re.search(rf"\[{tag}:([^\]]+)\]", full_reply)
            if m:
                session.set(tag.lower(), m.group(1).strip())
 
        hm      = _HANDOFF_RE.search(full_reply)
        handoff = None
        if hm and hm.group(1) in self.can_handoff_to:
            handoff = HandoffSignal(target=hm.group(1))
 
        end_call   = "[END_CALL]" in full_reply
        clean_text = _REPLY_CLEAN_RE.sub("", full_reply).strip()
        yield None, AgentResponse(text=clean_text, handoff=handoff, end_call=end_call)
