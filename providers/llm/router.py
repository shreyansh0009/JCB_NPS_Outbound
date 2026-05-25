"""
Sprint 3: Dynamic model router — selects fast 8b vs smart 70b per-turn.

Routing rationale:
  Indian language voice calls have two fundamentally different turn types:

  FAST turns (llama-3.1-8b-instant, ~750 tok/s, ~80ms TTFB):
    - Opening / closing pleasantries
    - Data collection: user providing name, phone, address
    - Short confirmations: "haan", "theek hai", "ok", "yes please"
    - Turn 1 of any call (always a greeting response)

  SMART turns (llama-3.3-70b-versatile, ~280 tok/s, ~200ms TTFB):
    - Product fault diagnosis (not cooling, noise, leaking, error codes)
    - Warranty / guarantee questions
    - Price / model comparison / recommendations
    - Long user messages (> 25 words) — signals complexity
    - Deep in conversation (turn 5+) where context matters most
    - Any question mark in an Indian script (genuine diagnostic question)

DynamicGroqLLM is a drop-in replacement for GroqLLM — same BaseLLM interface.
It holds both model instances and picks per-call, adding negligible overhead.
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)

# ── Complexity signal patterns ────────────────────────────────────────────────

_COMPLEX_RE = re.compile(
    r"""
    # English technical / complaint / comparison
    \b(?:
        warranty|guarantee|repair|replace|refund|escalat|
        not\s+work|not\s+cool|not\s+heat|not\s+start|
        leaking?|noise|vibrat|smells?|burn|blink|flash|dead|damage|
        error|code|problem|issue|fault|
        how\s+much|price|cost|rate|compare|which\s+model|best|recommend|
        when\s+will|how\s+long|how\s+soon
    )\b
    |
    # Hindi / Hinglish technical
    \b(?:
        kharab|band\s+ho|nahi\s+chal|chalu\s+nahi|thanda\s+nahi|
        garam\s+nahi|awaaz|darr|toot|leak|paani|
        warranty|guarantee|repair|
        shikayat|complaint|
        kitna|konsa|kaunsa|kaisa|suggest|recommend|
        kab\s+tak|kitne\s+din|kitni\s+der
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Script chars: any Indian Unicode script indicates non-trivial language context
_INDIAN_SCRIPT_RE = re.compile(r"[\u0900-\u0D7F]")
_QUESTION_RE      = re.compile(r"[?？]")


def _is_complex(messages: list[dict]) -> bool:
    """
    Analyse the message list to decide whether to use the 70b model.
    Returns True → use 70b (smart).   Returns False → use 8b (fast).
    """
    # Extract last user message
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break

    if not user_msg or user_msg.startswith("[CALL CONTEXT:") or user_msg == "[CALL_START]":
        return False  # system markers — not real user turns

    word_count = len(user_msg.split())

    # Very short (≤ 8 words): high chance it's a confirmation or data input → fast
    if word_count <= 8:
        return False

    # Long message (> 25 words): complexity signal → smart
    if word_count > 25:
        return True

    # Contains known complex keywords
    if _COMPLEX_RE.search(user_msg):
        return True

    # Indian script + question mark → diagnostic question → smart
    if _INDIAN_SCRIPT_RE.search(user_msg) and _QUESTION_RE.search(user_msg):
        return True

    # Deep into conversation (8+ user turns) → use smart for richer context handling.
    # Threshold raised from 5→8: hello (2-3 turns) + screener (1 turn) are carried in
    # history via carry_history=True, so service/sales agents start with 3-4 pre-existing
    # user turns. 8 means the user has asked 4-5 real service questions — truly deep.
    user_turns = sum(1 for m in messages if m.get("role") == "user")
    if user_turns >= 8:
        return True

    return False


# ── DynamicGroqLLM ────────────────────────────────────────────────────────────

class DynamicGroqLLM(BaseLLM):
    """
    Transparent wrapper around two GroqLLM instances.

    At each call it inspects the messages, picks the right model, and
    delegates.  The overhead is a single regex pass on the last user
    message — negligible compared to network latency.

    Example (in agents/registry.py):
        from providers.llm.router import DynamicGroqLLM
        service_llm = DynamicGroqLLM(
            fast_llm=_make_llm(settings, "llama-3.1-8b-instant"),
            smart_llm=_make_llm(settings, "llama-3.3-70b-versatile"),
        )
    """

    def __init__(self, fast_llm: BaseLLM, smart_llm: BaseLLM):
        self._fast  = fast_llm
        self._smart = smart_llm

    def _pick(self, messages: list[dict]) -> BaseLLM:
        use_smart = _is_complex(messages)
        chosen = self._smart if use_smart else self._fast
        model_name = getattr(chosen, "model", "?")
        logger.debug(f"[Router] model={model_name} complex={use_smart}")
        self._last_model_used = chosen  # exposed for latency logging in agents
        return chosen

    async def chat(self, messages: list[dict]) -> str:
        return await self._pick(messages).chat(messages)

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        async for token in self._pick(messages).stream_chat(messages):
            yield token
