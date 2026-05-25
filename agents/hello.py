"""
HelloAgent: first contact — greets caller, gets name + mobile, hands off to screener.
"""
from __future__ import annotations
 
import logging
import random
import re
import unicodedata
from collections import deque
 
from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.indian_numbers import parse_indian_mobile
from core.session import CallSession
 
logger = logging.getLogger(__name__)
 
 
def _message_similarity(msg1: str, msg2: str) -> float:
    """Compute similarity between two messages (0.0 to 1.0) using word-set intersection."""
    words1 = set(msg1.lower().split())
    words2 = set(msg2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    return intersection / union
 
 
# ── Mobile number extraction ──────────────────────────────────────────────────
_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "won": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4",
    "five": "5",
    "six": "6", "sex": "6", "sिक्स": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9",
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4",
    "पांच": "5", "पाँच": "5", "छह": "6", "सात": "7", "आठ": "8", "नौ": "9",
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
    "ek": "1", "do": "2", "teen": "3", "char": "4", "paanch": "5",
    "chhe": "6", "cheh": "6", "saat": "7", "aath": "8", "nau": "9",
}
 
_DEVANAGARI_DIGITS = str.maketrans(
    "०१२३४५६७८९",
    "0123456789",
)
 
# ── Digit-to-word tables (deterministic readback — never let LLM convert) ──
_DIGIT_TO_HINDI = {
    "0": "शून्य", "1": "एक", "2": "दो", "3": "तीन", "4": "चार",
    "5": "पाँच", "6": "छह", "7": "सात", "8": "आठ", "9": "नौ",
}
_DIGIT_TO_ENGLISH = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}
 
_HINDI_FILLERS = ["अच्छा...", "हम्म...", "ठीक है...", "जी..."]
_ENGLISH_FILLERS = ["Got it...", "Okay...", "Right...", "One moment..."]

# Demo/example numbers used in prompt illustrations must never leak into live
# calls unless they were actually provided by the caller (or caller ID).
_DEMO_MOBILE_BLACKLIST = {
    "9876543210",
    "9877750004",
    "9123456789",
    "9877750664",
    "9876543211",
    "9988776655",
}

# ── "Same number" detection ──────────────────────────────────────────────────
# STT produces mixed-script transcripts like "यही number है जिससे बात कर रहा हूं"
# so we need to match individual Hindi/English keywords, not just full phrases.
_SAME_NUMBER_PHRASES = [
    # -------------------- English --------------------
    "same number", "this number", "calling number", "current number",
    "number i am calling from", "number i'm calling from",
    "number i am calling with", "number i'm calling with",
    "the number i called from", "the number i called with",

    # More variations
    "use this number", "use the same number",
    "call me on this number", "reach me on this number",
    "you can call me on this number", "contact me on this number",
    "this is my number", "this is the number",
    "my current number", "my same number",
    "keep this number", "continue with this number",
    "no change in number", "don't change the number",
    "use my current number", "use my calling number",
    "the same one", "use the same one",
    "this one only", "this number only",
    "i'm using this number", "i am using this number",
    "this is the number i use",
    "call back on this number",
    "you already have my number",
    "use the number you have",
    "the number you're seeing",
    "same as before",
    "no need to change number",

    # -------------------- Romanized Hindi --------------------
    "isi number", "yahi number", "yehi number", "issi number", "wahi number",
    "jis number se call", "jis number se baat",
    "isi number se", "yahi number se", "yehi number se", "wahi number se",

    # More variations
    "isi number pe", "yahi number pe", "yehi number pe",
    "isi number par", "yahi number par",
    "isi number use karo", "yahi number use karo",
    "isi number rakho", "yahi number rakho",
    "number same hai", "same number hai",
    "mera number yahi hai", "mera number isi hai",
    "main isi number se bol raha hu", "main isi number se bol rahi hu",
    "main yahi number use kar raha hu", "main yahi number use kar rahi hu",
    "isi number se contact karo", "yahi number se contact karo",
    "isi number pe call karo", "yahi number pe call karo",
    "isi number pe hi call karna", "yahi number pe hi call karna",
    "isi number pe baat karo", "yahi number pe baat karo",
    "number change nahi karna", "number mat badlo",
    "number same rakho", "same number rakho",
    "jo number hai wahi sahi hai",
    "jo number se call kiya wahi",
    "yeh mera current number hai",
    "isi se baat kar raha hu",
    "isi se baat kar rahi hu",
    "isi number se hi baat ho rahi hai",
    "isi number ko use karo",
    "isi number ko hi rakho",

    # -------------------- Devanagari Hindi --------------------
    "इसी नंबर", "यही नंबर", "येही नंबर", "वही नंबर",
    "जिस नंबर से कॉल", "जिस नंबर से बात",
    "इसी नंबर से", "यही नंबर से", "वही नंबर से",

    # More variations
    "इसी नंबर पर", "यही नंबर पर",
    "इसी नंबर पे", "यही नंबर पे",
    "इसी नंबर का उपयोग करें", "यही नंबर का उपयोग करें",
    "इसी नंबर को रखें", "यही नंबर को रखें",
    "नंबर वही है", "नंबर सेम है",
    "मेरा नंबर यही है", "मेरा नंबर इसी है",
    "मैं इसी नंबर से बात कर रहा हूँ", "मैं इसी नंबर से बात कर रही हूँ",
    "मैं यही नंबर इस्तेमाल कर रहा हूँ", "मैं यही नंबर इस्तेमाल कर रही हूँ",
    "इसी नंबर पर कॉल करें", "यही नंबर पर कॉल करें",
    "इसी नंबर पर ही कॉल करना", "यही नंबर पर ही कॉल करना",
    "इसी नंबर पर बात करें", "यही नंबर पर बात करें",
    "नंबर बदलना नहीं है", "नंबर मत बदलो",
    "नंबर वही रखना", "सेम नंबर रखना",
    "जो नंबर है वही सही है",
    "जिस नंबर से कॉल किया वही",
    "यह मेरा वर्तमान नंबर है",
    "इसी से बात कर रहा हूँ",
    "इसी से बात कर रही हूँ",
    "इसी नंबर से ही बात हो रही है",
    "इसी नंबर का उपयोग करें",

    # -------------------- Mixed (Devanagari + English) --------------------
    "यही number", "इसी number", "येही number", "वही number",
    "जिससे बात कर रह", "जिससे call कर रह",
    "जिस number से", "जिस number",

    # More variations
    "यही number use karo", "इसी number use karo",
    "यही number pe call karo", "इसी number pe call karo",
    "यही number pe baat karo", "इसी number pe baat karo",
    "यही number rakho", "इसी number rakho",
    "number wahi hai", "number same hai",
    "mera number यही है",
    "main isi number use kar raha hu",
    "main isi number use kar rahi hu",
    "isi number par call karein",
    "yahi number par call karein",
    "isi number ko use karein",
    "yahi number ko use karein",
    "number change mat karo",
    "same number hi rakho",
    "isi number ko hi use karo",
    "yahi number ko hi use karo",
]


# Regex patterns for broader matching (handles varied STT transcriptions)
_SAME_NUMBER_PATTERNS = [
    # "यही/इसी/येही" + "number/नंबर" anywhere in text
    re.compile(r"(?:यही|इसी|येही|वही|isi|yahi|yehi|wahi)\s*(?:number|नंबर)", re.IGNORECASE),
    # "same/calling/this" + "number" (English)
    re.compile(r"(?:same|calling|this|current)\s+number", re.IGNORECASE),
    # "जिससे बात/call कर रहा/रही" (the number I'm talking on)
    re.compile(r"जिससे\s+(?:बात|call|कॉल)\s+कर\s+रह", re.IGNORECASE),
    # "jis se baat/call kar raha" (romanized)
    re.compile(r"jis\s*se\s+(?:baat|call)\s+kar\s+rah", re.IGNORECASE),
]

_SAME_INTENT_TOKENS = {
    "same", "सेम", "yahi", "yehi", "isi", "issi", "wahi",
    "यही", "येही", "इसी", "वही", "this", "current", "calling",
}
_NUMBER_CONTEXT_TOKENS = {"number", "नंबर", "mobile", "फोन", "call", "कॉल"}
_DIFFERENT_INTENT_TOKENS = {
    "different", "new", "another", "other", "change", "replace",
    "dusra", "doosra", "naya", "alag",
    "दूसरा", "दुसरा", "नया", "अलग", "बदल",
}


def _is_same_number_request(text: str) -> bool:
    """Check if the user is referring to the number they are calling from."""
    normalized = unicodedata.normalize("NFKC", text or "")
    lower = text.lower()
    if any(phrase in lower for phrase in _SAME_NUMBER_PHRASES):
        return True
    if any(phrase in normalized for phrase in _SAME_NUMBER_PHRASES):
        return True
    if any(pat.search(normalized) for pat in _SAME_NUMBER_PATTERNS):
        return True

    # Dynamic intent heuristic for unseen caller phrasings like:
    # "नहीं यही same", "same hi", "issi", "this current one".
    low = re.sub(r"[^0-9a-z\u0900-\u097F\s]", " ", normalized.lower())
    tokens = [tok for tok in low.split() if tok]
    token_set = set(tokens)

    same_hits = sum(1 for t in _SAME_INTENT_TOKENS if t in token_set)
    has_number_context = any(t in token_set for t in _NUMBER_CONTEXT_TOKENS)
    has_different_intent = any(t in token_set for t in _DIFFERENT_INTENT_TOKENS)

    # Strong: multiple same-intent words and no "different/new number" intent.
    if same_hits >= 2 and not has_different_intent:
        return True

    # Medium: one same-intent word with explicit number/call context.
    if same_hits >= 1 and has_number_context and not has_different_intent:
        return True

    # Special mixed acknowledgement pattern seen in live calls:
    # "नहीं यही same" (caller means "not different, keep same one").
    if "same" in token_set and ({"यही", "इसी", "wahi", "yahi", "isi"} & token_set):
        return True
    return False


def _looks_like_keep_current_number(text: str) -> bool:
    """
    Broad same-number intent detector for natural caller phrasing.
    Used as a safety net so we don't miss variants and fall into LLM.
    """
    if _is_same_number_request(text):
        return True

    normalized = unicodedata.normalize("NFKC", text or "")
    low = re.sub(r"[^0-9a-z\u0900-\u097F\s]", " ", normalized.lower())
    tokens = [tok for tok in low.split() if tok]
    token_set = set(tokens)

    keep_tokens = {
        "same", "सेम", "yahi", "yehi", "isi", "issi", "wahi",
        "यही", "येही", "इसी", "वही", "current", "calling", "this",
        "ye", "yeh", "यहीहै", "यस्सेम",
    }
    different_tokens = _DIFFERENT_INTENT_TOKENS

    has_keep = any(t in token_set for t in keep_tokens)
    has_different = any(t in token_set for t in different_tokens)

    # Caller says "नहीं यही same" / "no same one" style correction.
    if has_keep and ("नहीं" in token_set or "no" in token_set or "nahin" in token_set):
        return True

    if has_keep and not has_different:
        return True
    return False


_ISSUE_HINTS: tuple[str, ...] = (
    "not starting", "not start", "breakdown", "service", "repair", "issue", "problem",
    "car", "bike", "scooter", "vehicle", "engine", "battery", "puncture", "stuck",
    "चल नहीं", "चालू नहीं", "स्टार्ट नहीं", "स्टार्ट नही", "गाड़ी", "गाड़ी", "बाइक",
    "स्कूटर", "वाहन", "ब्रेकडाउन", "सर्विस", "समस्या", "खराब",
    "band padli", "chalat nahi", "service havi", "problem aahe",
    "nadavatledu", "start avvatledu", "service kavali",
    "odala", "start aagala", "service venum",
)


def _looks_like_issue_statement(text: str) -> bool:
    norm = unicodedata.normalize("NFKC", text or "").strip()
    if not norm:
        return False
    low = norm.lower()
    if len(re.sub(r"\s+", "", norm)) < 6:
        return False
    return any(h in low or h in norm for h in _ISSUE_HINTS)
 
 
def _digits_to_words(digits: str, lang: str = "hi") -> str:
    """Convert '9472956565' → 'नौ चार सात दो नौ पाँच छह पाँच छह पाँच' (deterministic)."""
    table = _DIGIT_TO_HINDI if lang == "hi" else _DIGIT_TO_ENGLISH
    return " ".join(table[d] for d in digits if d in table)
 
 
def _extract_structured_mobile(text: str) -> str | None:
    match = re.search(r"\[PARSED_MOBILE:(\d{10})\]", text)
    if match:
        return match.group(1)
    return None


def _extract_mobile_candidates(text: str) -> set[str]:
    """Extract all 10-digit Indian-mobile-like strings from text."""
    normalized = unicodedata.normalize("NFKC", text or "").translate(_DEVANAGARI_DIGITS)
    return set(re.findall(r"(?<!\d)([6-9]\d{9})(?!\d)", normalized))


def _find_demo_mobile_leak(text: str, allowed_numbers: set[str]) -> str | None:
    """
    Return a leaked demo number if present in text and not in allowed_numbers.
    """
    for num in _extract_mobile_candidates(text):
        if num in _DEMO_MOBILE_BLACKLIST and num not in allowed_numbers:
            return num
    return None


def _extract_mobile_tag_value(text: str) -> str | None:
    """Extract [MOBILE:...] tag value and normalize Devanagari digits."""
    m = re.search(r"\[MOBILE:([^\]]+)\]", text or "")
    if not m:
        return None
    raw = unicodedata.normalize("NFKC", m.group(1)).translate(_DEVANAGARI_DIGITS)
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        for i in range(0, len(digits) - 9):
            candidate = digits[i:i + 10]
            if re.fullmatch(r"[6-9]\d{9}", candidate):
                return candidate
    return None
 
 
def _parse_mobile(text: str) -> str | None:
    """
    Extract a 10-digit Indian mobile number from a transcript.
 
    Handles:
      - Digit form:  "9876543210" or "(947) 295-6565"
      - Word form:   "nine eight seven six five four three two one zero"
      - Hindi digit words: "नौ आठ सात ..." or romanized Hindi "nau aath saat ..."
      - Country code: "91 9876543210" → strips leading 91
    Robust against pauses, fillers, and stuttering.
    Returns the 10-digit string or None if not found.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(_DEVANAGARI_DIGITS)
    # Undo Deepgram time-format artifacts (e.g., "09:02" → "9 0 2")
    normalized = re.sub(
        r'\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b',
        lambda m: ' '.join(
            [m.group(1).lstrip('0') or '0'] +
            list(m.group(2)) +
            (list(m.group(3)) if m.group(3) else [])
        ),
        normalized,
    )
 
    text_num = normalized.lower()
    sorted_digit_words = sorted(_DIGIT_WORDS.items(), key=lambda x: len(x[0]), reverse=True)
    for word, digit in sorted_digit_words:
        text_num = re.sub(rf"\b{re.escape(word)}\b", digit, text_num)
        
    digits = "".join(re.findall(r"\d+", text_num))
    
    if digits.startswith("91") and len(digits) >= 12:
        if digits[2:12][0] in "6789":
            digits = digits[2:]
    elif digits.startswith("0") and len(digits) >= 11:
        if digits[1:11][0] in "6789":
            digits = digits[1:]
            
    matches = re.findall(r"[6-9]\d{9}", digits)
    if matches:
        return matches[-1]
 
    return None
 
 
class HelloAgent(BaseAgent):
    name = "hello"
    can_handoff_to = ["screener", "closer"]
 
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_responses = deque(maxlen=5)  # Track last 5 responses for loop detection
 
    @staticmethod
    def _is_negative_confirmation(text: str) -> bool:
        # Only treat as "mobile rejection" when correction intent is explicit.
        # Do NOT classify general complaint sentences with "नहीं" as rejection.
        normalized = unicodedata.normalize("NFKC", text or "")
        lower = normalized.lower()
        explicit_reject = [
            "wrong", "galat", "not correct", "wrong number", "incorrect",
            "गलत", "गलत है", "सही नहीं", "गलत नंबर", "नंबर गलत",
            "phir se", "dobara", "repeat", "again", "फिर से", "दोबारा",
        ]
        if any(p in lower or p in normalized for p in explicit_reject):
            return True

        has_negative = bool(
            re.search(r"\b(?:no|nahin|nahi)\b", lower)
            or ("नहीं" in normalized)
            or ("नही" in normalized)
        )
        has_number_context = any(
            p in lower or p in normalized
            for p in ("number", "mobile", "नंबर", "मोबाइल", "phone", "फोन")
        )
        return has_negative and has_number_context

    @staticmethod
    def _is_repeat_readback_request(text: str) -> bool:
        """
        Detect "say the number again" style requests while waiting on mobile
        confirmation. In this case we should repeat the same captured number,
        not ask for a new one and not defer to LLM.
        """
        repeat_phrases = [
            # English / Romanized Hindi
            "repeat", "again", "say again", "read again", "once more",
            "phir se", "dobara", "wapis bolo", "wapas bolo",
            # Devanagari Hindi
            "वापस बोलो", "वापिस बोलो", "फिर से", "दोबारा",
        ]
        lower = text.lower()
        return any(phrase in lower for phrase in repeat_phrases) or \
               any(phrase in text for phrase in repeat_phrases)
 
    @staticmethod
    def _is_positive_confirmation(text: str) -> bool:
        positive_phrases = [
            # Romanized Hindi / English
            "yes", "haan", "ji", "correct", "sahi", "theek hai",
            "thik hai", "ok", "okay", "done", "right", "yes please",
            # Devanagari Hindi (STT transcribes Hindi speech in Devanagari)
            "हाँ", "हां", "हाँ", "जी", "सही", "ठीक", "बिल्कुल", "बिलकुल",
            "हाँ जी", "जी हाँ", "जी हां", "सही है", "ठीक है",
        ]
        lower = text.lower()
        # Check lowercased romanized; check original for Devanagari (case-insensitive doesn't apply)
        return any(phrase in lower for phrase in positive_phrases) or \
               any(phrase in text for phrase in positive_phrases)
 
    @staticmethod
    def _mobile_is_valid(mobile: str) -> bool:
        if not mobile:
            return False
        normalized = unicodedata.normalize("NFKC", mobile).translate(_DEVANAGARI_DIGITS)
        digits = re.sub(r"\D", "", normalized)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return bool(re.fullmatch(r"[6-9]\d{9}", digits))

    @staticmethod
    def _normalize_mobile(mobile: str) -> str:
        normalized = unicodedata.normalize("NFKC", mobile or "").translate(_DEVANAGARI_DIGITS)
        digits = re.sub(r"\D", "", normalized)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    def _build_mobile_confirmation(self, mobile: str, lang: str) -> str:
        digits = self._normalize_mobile(mobile)
        digit_words = _digits_to_words(digits, lang)
        if lang == "hi":
            filler = random.choice(_HINDI_FILLERS)
            return f"{filler} {digit_words}। क्या यह आपका मोबाइल नंबर है?"
        filler = random.choice(_ENGLISH_FILLERS)
        return f"{filler} {digit_words}. Is that your mobile number?"
 
    def _confirm_again_prompt(self, session: CallSession) -> str:
        session.set("mobile_confirmed_pending", False)
        lang = session.get("language", session.current_language)
        if lang == "hi":
            return "धन्यवाद, कृपया अपना मोबाइल नंबर एक बार फिर बताइए? मैं ध्यान से नोट कर रही हूँ।"
        return "Thank you. Could you please share your mobile number once again? I will note it carefully."
 
    def _preprocess_transcript(self, transcript: str, session) -> str:
        """
        If the transcript contains a mobile number, extract it and inject it
        as a structured [PARSED_MOBILE:xxx] note so the LLM copies the exact
        digits instead of re-interpreting them (which causes hallucinated numbers).
        """
        lang = session.get("language", session.current_language) or "hi"
        mobile_res = parse_indian_mobile(transcript, lang)
        mobile = mobile_res.digits
        if mobile:
            session.set("stated_mobile_raw", mobile)
            if mobile_res.readback:
                session.set("stated_mobile_readback", mobile_res.readback)
            return (
                f"{transcript}\n"
                f"[PARSED_MOBILE:{mobile}]"
            )
        return transcript
 
    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        # Outbound calls: skip inbound mobile-capture logic entirely, let LLM drive
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            reply = await self._chat(session, transcript)
            handoff = self._parse_handoff(reply)
            end_call = "[END_CALL]" in reply
            clean_reply = re.sub(
                r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|PARSED_MOBILE|INTENT):[^\]]*\]|\[END_CALL\]",
                "", reply,
            ).strip()
            if handoff == "screener":
                # Return empty text — ScreenerAgent opens with its own greeting,
                # so emitting any thanks here causes a double thank-you.
                return AgentResponse(text="", handoff=HandoffSignal(target="screener"))
            if handoff == "closer":
                # HelloAgent already contains the goodbye text in clean_reply.
                # Chaining to CloserAgent would cause it to generate a second
                # farewell on top of the one HelloAgent just spoke — audible echo.
                # End the call directly if we have text; let closer speak if not.
                if clean_reply:
                    return AgentResponse(text=clean_reply, end_call=True)
                return AgentResponse(text="", handoff=HandoffSignal(target="closer"))
            if handoff:
                return AgentResponse(text=clean_reply, handoff=HandoffSignal(target=handoff))
            if end_call:
                return AgentResponse(text=clean_reply, end_call=True)
            return AgentResponse(text=clean_reply)

        lang = session.get("language", session.current_language) or "hi"
        mobile_res = parse_indian_mobile(transcript, lang)
        parsed_mobile = _extract_structured_mobile(transcript) or mobile_res.digits
        if parsed_mobile:
            parsed_mobile = self._normalize_mobile(parsed_mobile)
        fallback_mobile = (
            self._normalize_mobile(session.get("stated_mobile_raw", ""))
            or self._normalize_mobile(session.get("mobile", ""))
            or self._normalize_mobile(session.get("caller_number", ""))
        )
        same_intent = _looks_like_keep_current_number(transcript)
        allowed_numbers = {
            n for n in {
                parsed_mobile,
                self._normalize_mobile(session.get("stated_mobile_raw", "")),
                self._normalize_mobile(session.get("mobile", "")),
                self._normalize_mobile(session.get("caller_number", "")),
            }
            if n and isinstance(n, str) and self._mobile_is_valid(n)
        }

        # If caller asks to repeat, always replay the traced/captured number
        # deterministically (never defer to LLM for this).
        if self._is_repeat_readback_request(transcript) and fallback_mobile and self._mobile_is_valid(fallback_mobile):
            session.set("stated_mobile_raw", fallback_mobile)
            session.set("mobile_confirmed_pending", True)
            repeat_confirmation = self._build_mobile_confirmation(fallback_mobile, lang)
            session.add_message("user", transcript)
            session.add_message("assistant", repeat_confirmation)
            logger.info(f"[REPEAT READBACK STRICT] Repeating mobile={fallback_mobile}")
            return AgentResponse(text=repeat_confirmation)

        # Safety net: any broad "keep same number" intent should use the traced
        # number directly and bypass LLM.
        if not parsed_mobile and same_intent and fallback_mobile and self._mobile_is_valid(fallback_mobile):
            parsed_mobile = fallback_mobile
            logger.info(f"[SAME NUMBER SAFETY] Using captured/caller number: {fallback_mobile}")

        # ── AGGRESSIVE ISSUE CAPTURE ──
        # If the caller states their issue early (with or without mobile), capture it
        # so we don't ask "How can I help you?" again in Screener/Service agents.
        if not session.get("intent") and not session.get("reported_issue"):
            clean_text = re.sub(r'\d+', '', transcript).lower()
            ignore_words = set(_DIGIT_WORDS.keys()).union({
                "haan", "yes", "ji", "sahi", "theek", "ok", "okay", "right", "correct", "bilkul",
                "हां", "हाँ", "जी", "सही", "ठीक", "बिल्कुल", "hai", "is", "am", "are", "the"
            })
            substantive_words = [w for w in clean_text.split() if w not in ignore_words]
            if len(" ".join(substantive_words)) > 5:
                session.set("reported_issue", transcript)
                logger.info(f"[HELLO] Captured reported_issue from early utterance: '{transcript}'")

        # ── "Same number" shortcut: use caller ID from Asterisk ──────────
        if not parsed_mobile and _is_same_number_request(transcript):
            caller_number = session.get("caller_number")
            logger.info(f"[SAME NUMBER] Detected 'same number' request. caller_number in session: {caller_number!r}")
            if caller_number and self._mobile_is_valid(caller_number):
                parsed_mobile = caller_number
                logger.info(f"[SAME NUMBER] Using caller number from Asterisk: {caller_number}")
            else:
                logger.warning(f"[SAME NUMBER] Caller ID missing/invalid ({caller_number!r}) — asking user for number.")
                if lang == "hi":
                    fallback_msg = "माफ़ कीजिएगा, सिस्टम में आपका नंबर नहीं आ रहा है। कृपया अपना 10 अंकों का मोबाइल नंबर बताइए?"
                else:
                    fallback_msg = "I'm sorry, I couldn't capture your caller ID. Could you please tell me your 10-digit mobile number?"
                session.add_message("user", transcript)
                session.add_message("assistant", fallback_msg)
                return AgentResponse(text=fallback_msg)

        if parsed_mobile:
            session.set("stated_mobile_raw", parsed_mobile)
            if mobile_res and mobile_res.readback:
                session.set("stated_mobile_readback", mobile_res.readback)
 
        # ── Deterministic confirmation: bypass LLM when mobile detected ─────
        # LLMs hallucinate when converting digits to Hindi/English words.
        # Code-generated readback is 100% accurate and saves an LLM round-trip.
        if parsed_mobile and self._mobile_is_valid(parsed_mobile) and not session.get("mobile_confirmed_pending"):
            session.set("mobile_confirmed_pending", True)
            confirmation = self._build_mobile_confirmation(parsed_mobile, lang)
            # Capture any [INTENT:] tag the LLM may have added via _preprocess_transcript path,
            # or extract from an inline [INTENT:] already in the transcript (edge case).
            if not session.get("intent"):
                intent_m = re.search(r"\[INTENT:\s*([^\]]+)\]", transcript)
                if intent_m:
                    session.set("intent", intent_m.group(1).strip())
            session.add_message("user", transcript)
            session.add_message("assistant", confirmation)
            logger.info(f"[DETERMINISTIC READBACK] mobile={parsed_mobile} (lang={lang})")
            return AgentResponse(text=confirmation)
 
        # ── Deterministic handoff: bypass LLM when user responds to confirmation ─
        # When mobile_confirmed_pending is set, the agent is waiting for yes/no.
        # Handle entirely in code — no LLM needed for this binary decision.
        if session.get("mobile_confirmed_pending") and not parsed_mobile:
            if self._is_repeat_readback_request(transcript) and fallback_mobile and self._mobile_is_valid(fallback_mobile):
                session.set("stated_mobile_raw", fallback_mobile)
                repeat_confirmation = self._build_mobile_confirmation(fallback_mobile, lang)
                session.add_message("user", transcript)
                session.add_message("assistant", repeat_confirmation)
                logger.info(f"[REPEAT READBACK] Repeating captured mobile={fallback_mobile}")
                return AgentResponse(text=repeat_confirmation)

            # If caller starts describing the vehicle issue while mobile confirmation
            # is pending, treat it as implicit confirmation and move forward.
            if fallback_mobile and self._mobile_is_valid(fallback_mobile) and _looks_like_issue_statement(transcript):
                session.set("mobile", fallback_mobile)
                session.set("mobile_confirmed_pending", False)
                if not session.get("intent"):
                    session.set("intent", transcript)
                if not session.get("reported_issue"):
                    session.set("reported_issue", transcript)
                logger.info(
                    f"[IMPLICIT CONFIRM + ISSUE] mobile={fallback_mobile} intent='{(session.get('intent') or '')[:80]}'"
                )
                session.add_message("user", transcript)
                session.add_message("assistant", "[HANDOFF:screener]")
                return AgentResponse(text="", handoff=HandoffSignal(target="screener"))

            if self._is_negative_confirmation(transcript):
                logger.info(f"[NEGATIVE CONFIRMATION] '{transcript[:50]}' — re-asking mobile")
                session.add_message("user", transcript)
                re_ask = self._confirm_again_prompt(session)
                session.add_message("assistant", re_ask)
                return AgentResponse(text=re_ask)
 
            if self._is_positive_confirmation(transcript) or same_intent:
                mobile = session.get("stated_mobile_raw", "")
                if mobile and self._mobile_is_valid(mobile):
                    session.set("mobile", mobile)
                    session.set("mobile_confirmed_pending", False)
                    name = session.get("name", "")
                    intent = session.get("intent", "")
                    logger.info(f"[DETERMINISTIC HANDOFF] mobile={mobile} name={name} intent={intent}")
                    session.add_message("user", transcript)
                    handoff_msg = f"[HANDOFF:screener] [NAME:{name}] [MOBILE:{mobile}]"
                    if intent:
                        handoff_msg += f" [INTENT:{intent}]"
                    session.add_message("assistant", handoff_msg)
                    return AgentResponse(
                        text="",
                        handoff=HandoffSignal(target="screener"),
                    )
            # Ambiguous response while awaiting confirmation:
            # re-confirm the same traced/captured number deterministically.
            if fallback_mobile and self._mobile_is_valid(fallback_mobile):
                session.set("stated_mobile_raw", fallback_mobile)
                re_confirm = self._build_mobile_confirmation(fallback_mobile, lang)
                session.add_message("user", transcript)
                session.add_message("assistant", re_confirm)
                logger.info(f"[AMBIGUOUS CONFIRMATION] Re-confirming captured mobile={fallback_mobile}")
                return AgentResponse(text=re_confirm)
            # No known valid number to re-confirm — ask again.
            return AgentResponse(text=self._confirm_again_prompt(session))
 
        reply = await self._chat(session, transcript)

        # Final guard: if caller intent was "same number", never trust LLM text.
        if same_intent and fallback_mobile and self._mobile_is_valid(fallback_mobile):
            logger.warning(
                "[HELLO SAME-INTENT OVERRIDE] Ignoring LLM text and re-confirming traced mobile=%s",
                fallback_mobile,
            )
            session.set("stated_mobile_raw", fallback_mobile)
            session.set("mobile_confirmed_pending", True)
            return AgentResponse(text=self._build_mobile_confirmation(fallback_mobile, lang))

        leaked_demo = _find_demo_mobile_leak(reply, allowed_numbers)
        if leaked_demo:
            logger.warning(
                "[HELLO STATIC MOBILE GUARD] blocked demo number '%s' in LLM reply; "
                "allowed=%s transcript='%s'",
                leaked_demo,
                sorted(allowed_numbers),
                transcript[:80],
            )
            return AgentResponse(text=self._confirm_again_prompt(session))

        # If LLM emits a [MOBILE:...] tag with an unexpected/random number while
        # we already have traced/captured number, force deterministic reconfirm.
        tagged_mobile = _extract_mobile_tag_value(reply)
        if tagged_mobile and fallback_mobile and self._mobile_is_valid(fallback_mobile):
            if tagged_mobile != fallback_mobile and tagged_mobile not in allowed_numbers:
                logger.warning(
                    "[HELLO MOBILE TAG MISMATCH] tagged=%s fallback=%s allowed=%s; forcing deterministic confirmation",
                    tagged_mobile,
                    fallback_mobile,
                    sorted(allowed_numbers),
                )
                session.set("stated_mobile_raw", fallback_mobile)
                session.set("mobile_confirmed_pending", True)
                return AgentResponse(text=self._build_mobile_confirmation(fallback_mobile, lang))
 
        # ── Always extract name/intent tags from every LLM reply ──────────
        # NAME is emitted in step 2 (when acknowledging name + asking for mobile).
        # Extracting here ensures session has name before the deterministic handoff fires.
        for tag, key in (("NAME", "name"), ("INTENT", "intent")):
            m = re.search(rf"\[{tag}:([^\]]+)\]", reply)
            if m and not session.get(key):  # don't overwrite an already-stored value
                session.set(key, m.group(1).strip())
 
        # ── Guard: LLM returned empty string ──────────────────────────────
        if not reply or not reply.strip():
            lang = session.get("language", "hi")
            fallback = (
                "क्षमा करें, क्या आप दोबारा बता सकते हैं?"
                if lang == "hi"
                else "I'm sorry, could you please repeat that?"
            )
            return AgentResponse(text=fallback)
 
        handoff     = self._parse_handoff(reply)
        end_call    = "[END_CALL]" in reply
        clean_reply = re.sub(
            r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|PARSED_MOBILE|INTENT|नाम|मोबाइल):[^\]]*\]|\[[^\]]+:[^\]]+\]|\[END_CALL\]",
            "",
            reply,
        ).strip()

        # Fallback: if LLM forgot to emit [HANDOFF] but we already have enough
        # details and caller issue is clear, move forward silently.
        final_mobile = self._normalize_mobile(
            session.get("mobile", "") or session.get("stated_mobile_raw", "") or fallback_mobile
        )
        if (
            not handoff
            and not end_call
            and session.get("name")
            and self._mobile_is_valid(final_mobile)
            and _looks_like_issue_statement(transcript)
        ):
            if not session.get("intent"):
                session.set("intent", transcript)
            if not session.get("reported_issue"):
                session.set("reported_issue", transcript)
            session.set("mobile", final_mobile)
            session.set("mobile_confirmed_pending", False)
            logger.info("[HELLO FALLBACK HANDOFF] Forcing silent handoff to screener (intent captured)")
            return AgentResponse(text="", handoff=HandoffSignal(target="screener"))
 
        if handoff:
            # ── CRITICAL: Block handoff on ANY negative confirmation ──
            # If user says "galat", "wrong", "phir se", "dobara", etc., reject handoff immediately.
            # Do NOT proceed to screener even if LLM emitted [HANDOFF].
            if self._is_negative_confirmation(transcript):
                logger.warning(f"[NEGATIVE CONFIRMATION] User said '{transcript[:60]}' — blocking handoff, re-asking mobile")
                return AgentResponse(text=self._confirm_again_prompt(session))
            
            # If not explicitly positive and not issue-like, keep confirmation strict.
            if (
                not self._is_positive_confirmation(transcript)
                and not same_intent
                and not _looks_like_issue_statement(transcript)
            ):
                logger.info(f"[MOBILE CONFIRMATION] Not confirmed yet: '{transcript[:60]}' — re-asking")
                return AgentResponse(text=self._confirm_again_prompt(session))
 
            logger.info(f"[POSITIVE CONFIRMATION] User accepted mobile — proceeding to handoff")
            name_match   = re.search(r"\[NAME:([^\]]+)\]", reply)
            mobile_match = re.search(r"\[MOBILE:([^\]]+)\]", reply)
            intent_match = re.search(r"\[INTENT:([^\]]+)\]", reply)
            if mobile_match or session.get("stated_mobile_raw"):
                mobile_value = (session.get("stated_mobile_raw") or mobile_match.group(1).strip())
                if not self._mobile_is_valid(mobile_value):
                    return AgentResponse(text=self._confirm_again_prompt(session))
                session.set("mobile", mobile_value)
            if name_match:
                session.set("name", name_match.group(1).strip())
            if intent_match:
                session.set("intent", intent_match.group(1).strip())
 
            return AgentResponse(
                text="",
                handoff=HandoffSignal(target=handoff)
            )
 
        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)
 
        return AgentResponse(text=clean_reply)
 
    async def stream_handle(self, transcript: str, session: CallSession):
        """Route through handle() to ensure deterministic mobile readback and confirmation guards."""
        resp = await self.handle(transcript, session)
        clean_text = (resp.text or "").strip()
        if clean_text:
            for part in re.split(r'(?<=[.!?।])\s+', clean_text):
                part = part.strip()
                if part:
                    yield part, None
        yield None, resp
 
    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
 
 
