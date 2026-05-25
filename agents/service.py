"""
ServiceAgent: handles complaints, service booking, warranty, and escalation.

MCP tools (from the DMS/scheduling API):
  get_service_slots(date, service_type) -> list of available times
  book_service_appointment(name, phone, date, time, service_type) -> confirmation

RAG: product knowledge base, service center info, warranty details.
"""
from __future__ import annotations

import logging
import re
import difflib
import random
from datetime import date, datetime, timedelta
import zoneinfo
from collections import deque

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.indian_numbers import parse_indian_pincode, NumericParseResult
from core.session import CallSession
from core.caller_pace_profiler import CallerPaceProfiler   # NEW

logger = logging.getLogger(__name__)


def _message_similarity(msg1: str, msg2: str) -> float:
    """Compute similarity between two messages (0.0 to 1.0)."""
    words1 = set(msg1.lower().split())
    words2 = set(msg2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    return intersection / union


def _is_short_confirmation(text: str) -> bool:
    cleaned = text.strip().lower()
    if not cleaned:
        return False
    return cleaned in {
        "yes", "haan", "ji", "ok", "okay", "right", "correct",
        "हाँ", "हां", "जी", "ठीक", "सही", "बिल्कुल",
        "हो", "होय",
        "అవును", "సరే",
        "ஆம்", "ஆமாம்", "சரி",
    }


def _is_short_negative(text: str) -> bool:
    cleaned = _normalize_address_text(text).lower()
    if not cleaned:
        return False
    return cleaned in {
        "no", "nope", "nah", "nahi", "nahin", "गलत", "नहीं", "नही",
        "नहीं सही", "not correct",
        "नाही",
        "కాదు", "కాదు కాదు",
        "இல்லை", "தவறு",
    }


# Keywords the LLM uses when asking the customer for their service address.
# When detected in an LLM reply, we set session["expecting_address"] so the
# pipeline extends the utterance-hold window to capture multi-part addresses
# ("Flat 107, Aravali homes, Gandhi path, Vaishali Nagar, Jaipur") in one go.
_ADDRESS_ASK_KEYWORDS: tuple[str, ...] = (
    "पूरा पता",
    "पूरा address",
    "service address",
    "full address",
    "complete address",
    "address बताइए",
    "address बताइये",
    "पता बताइए",
    "पता बताइये",
    "address क्या है",
    "पता क्या है",
    "आपका address",
    "आपका पता",
)


def _is_address_ask(reply: str) -> bool:
    low = reply.lower()
    return any(kw.lower() in low for kw in _ADDRESS_ASK_KEYWORDS)


# Free-form address component hints (Hindi + English). Used only as a light
# heuristic when we are already in address-capture context.
_ADDRESS_COMPONENT_HINTS: tuple[str, ...] = (
    "nagar", "road", "rd", "street", "st", "lane", "gali", "sector",
    "block", "phase", "colony", "society", "apartment", "apt", "tower",
    "flat", "floor", "building", "bldg", "house", "plot", "landmark",
    "near", "opposite", "behind", "front of",
    "नगर", "रोड", "गली", "सेक्टर", "ब्लॉक", "फेज", "कॉलोनी", "सोसाइटी",
    "अपार्टमेंट", "टावर", "फ्लैट", "मंजिल", "बिल्डिंग", "मकान", "प्लॉट",
    "लैंडमार्क", "पास", "सामने", "पीछे",
)

_ADDRESS_QUERY_HINTS: tuple[str, ...] = (
    "address kya", "pata kya", "what is my address", "mera address kya",
    "मेरा address क्या", "मेरा पता क्या", "पता क्या", "address क्या",
)

_NEGATIVE_CORRECTION_HINTS: tuple[str, ...] = (
    "no", "nahin", "nahi", "wrong", "galat", "not correct",
    "नहीं", "नही", "गलत", "सही नहीं",
)

_PINCODE_ASK_KEYWORDS: tuple[str, ...] = (
    "pincode",
    "pin code",
    "6 अंकों",
    "६ अंकों",
    "पिनकोड",
    "पिन कोड",
)

_SLOT_ASK_KEYWORDS: tuple[str, ...] = (
    "slot",
    "time convenient",
    "which time",
    "which day",
    "available",
    "visit",
    "कौन सा time",
    "कौन सा दिन",
    "किस दिन",
    "किस समय",
    "कितने बजे",
    "available हैं",
)

_SLOT_VALUE_HINTS: tuple[str, ...] = (
    "today", "tomorrow", "day after", "next", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday",
    "am", "pm", "morning", "evening", "afternoon",
    "aaj", "kal", "parso", "narso", "somvar", "mangal", "budh", "guru",
    "shukr", "shanivar", "ravivar",
    "आज", "कल", "परसों", "नरसों", "सोमवार", "मंगलवार", "बुधवार",
    "गुरुवार", "शुक्रवार", "शनिवार", "रविवार",
    "सुबह", "शाम", "दोपहर", "रात", "बजे",
    "ఈరోజు", "రేపు", "ఎల్లుండి",
    "आज", "उद्या", "परवा", "तरपरवा",
    "இன்று", "நாளை", "நாளை மறுநாள்",
)

_ENQUIRY_HINTS: tuple[str, ...] = (
    "kya", "kaise", "kab", "kahan", "kyun",
    "what", "how", "when", "where", "why",
    "detail", "details", "status", "update",
    "sr", "service request", "ticket",
    "address", "pincode", "pin code", "slot", "time", "date", "engineer",
    "क्या", "कैसे", "कब", "कहाँ", "कहां", "क्यों",
    "डिटेल", "जानकारी", "स्टेटस", "अपडेट", "पता", "पिनकोड", "पिन कोड",
    "स्लॉट", "समय", "डेट", "इंजीनियर",
)

_HANDOFF_TRANSITION_KEYWORDS: tuple[str, ...] = (
    "transfer", "transferring", "connect", "connecting", "route", "routing",
    "handoff", "switch you", "forward", "forwarding", "specialist team",
    "jod", "jodti", "jod raha", "jod rahi", "connect kar", "transfer kar",
    "route kar", "aage connect", "aage route",
    "जोड़", "जोड", "कनेक्ट", "ट्रांसफर", "रूट", "हैंडऑफ",
)

_SPOKEN_DIGIT_MAP: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ek": "1", "do": "2", "teen": "3", "char": "4", "chaar": "4",
    "paanch": "5", "panch": "5", "cheh": "6", "chhe": "6",
    "saat": "7", "aath": "8", "nau": "9",
    "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5",
    "छह": "6", "सात": "7", "आठ": "8", "नौ": "9", "शून्य": "0",
    "शुन्य": "0",
    "ఒకటి": "1", "రెండు": "2", "మూడు": "3", "నాలుగు": "4",
    "ఐదు": "5", "ఆరు": "6", "ఏడు": "7", "ఎనిమిది": "8", "తొమ్మిది": "9", "సున్నా": "0",
    "एक": "1", "दोन": "2", "तीन": "3", "चार": "4", "पाच": "5",
    "सहा": "6", "सात": "7", "आठ": "8", "नऊ": "9", "शून्य": "0",
    "ஒன்று": "1", "இரண்டு": "2", "மூன்று": "3", "நான்கு": "4",
    "ஐந்து": "5", "ஆறு": "6", "ஏழு": "7", "எட்டு": "8", "ஒன்பது": "9", "பூஜ்யம்": "0",
}

_REG_SPOKEN_DIGIT_WORDS: set[str] = set(_SPOKEN_DIGIT_MAP.keys())

_SPOKEN_LETTER_MAP: dict[str, str] = {
    "ay": "A", "a": "A", "ए": "A",
    "bee": "B", "b": "B", "बी": "B",
    "cee": "C", "c": "C", "सी": "C",
    "dee": "D", "d": "D", "डी": "D",
    "ee": "E", "e": "E", "ई": "E",
    "ef": "F", "f": "F", "एफ": "F",
    "gee": "G", "g": "G", "जी": "G",
    "aitch": "H", "h": "H", "एच": "H",
    "eye": "I", "i": "I", "आई": "I",
    "jay": "J", "j": "J", "जे": "J",
    "kay": "K", "k": "K", "के": "K",
    "el": "L", "l": "L", "एल": "L",
    "em": "M", "m": "M", "एम": "M",
    "en": "N", "n": "N", "एन": "N",
    "oh": "O", "o": "O", "ओ": "O",
    "pee": "P", "p": "P", "पी": "P",
    "cue": "Q", "queue": "Q", "q": "Q", "क्यू": "Q",
    "ar": "R", "r": "R", "आर": "R",
    "ess": "S", "s": "S", "एस": "S",
    "tee": "T", "t": "T", "टी": "T",
    "you": "U", "u": "U", "यू": "U",
    "vee": "V", "v": "V", "वी": "V",
    "doubleyou": "W", "w": "W", "डब्ल्यू": "W",
    "ex": "X", "x": "X", "एक्स": "X",
    "why": "Y", "y": "Y", "वाई": "Y",
    "zee": "Z", "zed": "Z", "z": "Z", "जेड": "Z",
}

_UNICODE_DIGIT_TRANSLATION = str.maketrans({
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
    "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4",
    "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",
    "௦": "0", "௧": "1", "௨": "2", "௩": "3", "௪": "4",
    "௫": "5", "௬": "6", "௭": "7", "௮": "8", "௯": "9",
})

_RELATIVE_DAY_TOKENS: dict[str, tuple[tuple[str, ...], ...]] = {
    "en": (
        ("today",),
        ("tomorrow",),
        ("day after tomorrow",),
        ("in three days", "after three days"),
        ("in four days", "after four days"),
    ),
    "hi": (
        ("आज", "aaj"),
        ("कल", "kal"),
        ("परसों", "परसो", "parso", "parson"),
        ("नरसों", "नरसो", "narso", "narson"),
        ("चरसों", "चरसो", "charso", "charson"),
    ),
    "te": (
        ("ఈరోజు", "eeroju"),
        ("రేపు", "repu"),
        ("ఎల్లుండి", "ellundi"),
        ("మూడో రోజు", "3 రోజుల్లో", "moodu rojullo"),
        ("నాలుగో రోజు", "4 రోజుల్లో", "nalugu rojullo"),
    ),
    "mr": (
        ("आज", "aaj"),
        ("उद्या", "udya"),
        ("परवा", "parva"),
        ("तरपरवा", "tarparva"),
        ("चार दिवसांनी", "char divasanni"),
    ),
    "ta": (
        ("இன்று", "indru"),
        ("நாளை", "naalai"),
        ("நாளை மறுநாள்", "naalai marunaal"),
        ("மூன்றாம் நாள்", "3 நாளில்"),
        ("நான்காம் நாள்", "4 நாளில்"),
    ),
}

_WEEKDAYS: dict[str, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "hi": ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
    "te": ("సోమవారం", "మంగళవారం", "బుధవారం", "గురువారం", "శుక్రవారం", "శనివారం", "ఆదివారం"),
    "mr": ("सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
    "ta": ("திங்கள்", "செவ்வாய்", "புதன்", "வியாழன்", "வெள்ளி", "சனி", "ஞாயிறு"),
}

_MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
    "hi": ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"),
    "te": ("జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్", "జూలై", "ఆగస్టు", "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్"),
    "mr": ("जानेवारी", "फेब्रुवारी", "मार्च", "एप्रिल", "मे", "जून", "जुलै", "ऑगस्ट", "सप्टेंबर", "ऑक्टोबर", "नोव्हेंबर", "डिसेंबर"),
    "ta": ("ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"),
}


def _normalize_address_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip(" ,.-")


def _looks_like_address_query(text: str) -> bool:
    norm = _normalize_address_text(text)
    if not norm:
        return False
    low = norm.lower()
    has_address_word = ("address" in low) or ("पता" in norm)
    has_query_word = (
        "?" in norm
        or "kya" in low
        or "what" in low
        or "kyun" in low
        or "why" in low
        or "बताओ" in norm
        or "बताना" in norm
        or "batao" in low
    )
    if has_address_word and has_query_word:
        return True
    return any(hint in low for hint in _ADDRESS_QUERY_HINTS)


def _looks_like_negative_correction(text: str) -> bool:
    norm = _normalize_address_text(text)
    if not norm:
        return False
    low = norm.lower()
    return any(hint in low for hint in _NEGATIVE_CORRECTION_HINTS)


def _looks_like_address_value(text: str) -> bool:
    norm = _normalize_address_text(text)
    if len(norm) < 6:
        return False
    if _looks_like_address_query(norm) or _is_short_confirmation(norm):
        return False

    # Avoid capturing pure numeric responses (typically pincode/house number only).
    if re.fullmatch(r"[\d\s,./-]+", norm):
        return False

    low = norm.lower()
    has_hint = any(hint in low for hint in _ADDRESS_COMPONENT_HINTS)
    has_digit = bool(re.search(r"\d", norm))
    has_separator = ("," in norm) or ("/" in norm) or ("-" in norm)
    token_count = len([tok for tok in norm.split(" ") if tok])

    # We stay permissive in address phase to support free-form formats.
    return has_hint or has_digit or has_separator or token_count >= 4


def _is_pincode_ask(reply: str) -> bool:
    low = (reply or "").lower()
    return any(kw in low for kw in _PINCODE_ASK_KEYWORDS)


def _is_enquiry_turn(text: str) -> bool:
    norm = _normalize_address_text(text)
    if not norm:
        return False
    low = norm.lower()
    return ("?" in norm) or any(h in low for h in _ENQUIRY_HINTS)


def _is_slot_ask(reply: str) -> bool:
    low = (reply or "").lower()
    return any(kw in low for kw in _SLOT_ASK_KEYWORDS)


def _normalize_registration_text(text: str) -> str:
    norm = _normalize_address_text(text)
    if not norm:
        return ""
    return norm.translate(_UNICODE_DIGIT_TRANSLATION)


def _format_registration(state: str, district: str, series: str, number: str) -> str | None:
    state_u = state.upper()
    series_u = series.upper()
    if len(state_u) != 2 or not district or district == "0":
        return None
    if len(series_u) < 1 or len(series_u) > 3:
        return None
    if len(number) < 3 or len(number) > 4:
        return None
    return f"{state_u} {district} {series_u} {number}"


def _registration_readback(registration: str) -> str:
    compact = re.sub(r"\s+", "", registration).upper()
    m = re.fullmatch(r"([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{3,4})", compact)
    if not m:
        return " ".join(list(compact))
    state, district, series, number = m.groups()
    return f"{' '.join(state)}, {' '.join(district)}, {' '.join(series)}, {' '.join(number)}"


def _registration_from_compact(compact: str) -> str | None:
    m = re.fullmatch(r"([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{3,4})", compact)
    if not m:
        return None
    state, district, series, number = m.groups()
    return _format_registration(state, district, series, number)


def _looks_like_registration_value(text: str) -> bool:
    """
    Detect Indian vehicle registration style utterances, including spoken forms:
    - "MH12AB1234"
    - "mh zero a b one two three four five six seven"
    """
    if _extract_registration_number(text):
        return True

    norm = _normalize_registration_text(text).lower()
    if not norm:
        return False

    if re.search(r"\b[a-z]{2}\s*\d{1,2}\s*[a-z]{1,3}\s*\d{3,4}\b", norm):
        return True

    compact = re.sub(r"[^a-z0-9]", "", norm)
    if re.fullmatch(r"[a-z]{2}\d{1,2}[a-z]{1,3}\d{3,4}", compact):
        return True

    tokens = [t for t in re.split(r"\s+", norm) if t]
    alpha_tokens = sum(
        1
        for t in tokens
        if re.fullmatch(r"[a-z]{1,3}", t) and t not in _REG_SPOKEN_DIGIT_WORDS
    )
    spoken_digit_tokens = sum(1 for t in tokens if t in _REG_SPOKEN_DIGIT_WORDS)
    return alpha_tokens >= 2 and spoken_digit_tokens >= 3 and len(tokens) >= 6


def _strip_handoff_transition_text(text: str) -> str:
    """
    Remove transfer/connect bridge phrases so handoffs stay silent.
    Keeps only non-transition sentences, if any.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?।])\s+", clean)
    kept: list[str] = []
    for part in parts:
        low = part.lower()
        if any(k in low for k in _HANDOFF_TRANSITION_KEYWORDS):
            continue
        kept.append(part.strip())
    return " ".join(p for p in kept if p).strip()


def _looks_like_slot_value(text: str) -> bool:
    norm = _normalize_address_text(text)
    if not norm:
        return False
    low = norm.lower()
    has_time_num = bool(re.search(r"\b([01]?\d|2[0-3])(?::[0-5]\d)?\b", low))
    has_hint = any(h in low for h in _SLOT_VALUE_HINTS)
    return has_time_num or has_hint


def _default_pending_prompt(step: str, lang: str) -> str:
    if step == "slot":
        if lang == "hi":
            return "कृपया अपनी preferred date और time बताइए, 8 बजे सुबह से 6 बजे शाम के बीच?"
        return "Please share your preferred date and time between 8 AM and 6 PM."
    if step == "address":
        if lang == "hi":
            return "अब कृपया अपना पूरा service address बताइए?"
        return "Could you please share your full service address?"
    if step == "pincode":
        if lang == "hi":
            return "और आपके area का 6 अंकों का pincode क्या है?"
        return "And what is your 6-digit pincode?"
    if step == "registration":
        return _registration_followup_prompt(lang)
    return ""


def _registration_followup_prompt(lang: str) -> str:
    if lang == "hi":
        return "आपका pincode note हो गया है। अब कृपया अपना vehicle registration number बताइए।"
    if lang == "te":
        return "మీ pincode note చేశాను. ఇప్పుడు దయచేసి మీ vehicle registration number చెప్పండి."
    if lang == "mr":
        return "तुमचा pincode note झाला आहे. आता कृपया तुमचा vehicle registration number सांगा."
    if lang == "ta":
        return "உங்கள் pincode note ஆகிவிட்டது. இப்போது உங்கள் vehicle registration number சொல்லுங்கள்."
    return "Your pincode is already noted. Please share your vehicle registration number now."


def _post_sr_slot_prompt(lang: str) -> str:
    if lang == "hi":
        return "अब SR number generate हो गया है। कृपया service visit के लिए कौन सा day और time convenient रहेगा?"
    if lang == "te":
        return "ఇప్పుడు SR number generate అయ్యింది. Service visit కోసం ఏ రోజు, ఏ time మీకు సౌకర్యంగా ఉంటుంది?"
    if lang == "mr":
        return "आता SR number तयार झाला आहे. Service visit साठी कोणता day आणि time तुम्हाला सोयीचा आहे?"
    if lang == "ta":
        return "இப்போது SR number generate ஆகிவிட்டது. Service visitக்கு எந்த நாள், எந்த time உங்களுக்கு வசதியாக இருக்கும்?"
    return "Now that the SR number is generated, which day and time is convenient for the service visit?"


def _format_local_date(target: date, lang: str) -> str:
    weekdays = _WEEKDAYS.get(lang, _WEEKDAYS["en"])
    months = _MONTHS.get(lang, _MONTHS["en"])
    weekday = weekdays[target.weekday()]
    month = months[target.month - 1]
    if lang == "en":
        return f"{weekday}, {target.day} {month}"
    return f"{weekday}, {target.day} {month}"


def _relative_day_offset(text: str, lang: str) -> tuple[int, str] | None:
    norm = _normalize_address_text(text)
    low = norm.lower()
    local_tokens = _RELATIVE_DAY_TOKENS.get(lang, ())
    for offset, group in enumerate(local_tokens):
        if any(tok and (tok in low or tok in norm) for tok in group):
            return offset, group[0]
    for offset, group in enumerate(_RELATIVE_DAY_TOKENS.get("en", ())):
        if any(tok and (tok in low or tok in norm) for tok in group):
            return offset, group[0]
    return None


def _extract_time_label(text: str) -> str | None:
    norm = _normalize_address_text(text)
    low = norm.lower()
    m = re.search(r"\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)?\b", low)
    if m:
        hh = int(m.group(1))
        mm = m.group(2) or "00"
        ampm = (m.group(3) or "").upper()
        if ampm:
            return f"{hh}:{mm} {ampm}"
        return f"{hh}:{mm}"
    # fallback contextual buckets
    if any(k in low or k in norm for k in ("morning", "सुबह", "ఉదయం", "सकाळ", "காலை")):
        return "morning"
    if any(k in low or k in norm for k in ("afternoon", "दोपहर", "మధ్యాహ్నం", "दुपार", "மதியம்")):
        return "afternoon"
    if any(k in low or k in norm for k in ("evening", "शाम", "సాయంత్రం", "संध्याकाळ", "மாலை")):
        return "evening"
    if any(k in low or k in norm for k in ("night", "रात", "రాత్రి", "रात्र", "இரவு")):
        return "night"
    return None


def _localize_time_label(time_label: str, lang: str) -> str:
    low = (time_label or "").lower().strip()
    if low not in {"morning", "afternoon", "evening", "night"}:
        return time_label
    labels = {
        "hi": {"morning": "सुबह", "afternoon": "दोपहर", "evening": "शाम", "night": "रात"},
        "te": {"morning": "ఉదయం", "afternoon": "మధ్యాహ్నం", "evening": "సాయంత్రం", "night": "రాత్రి"},
        "mr": {"morning": "सकाळ", "afternoon": "दुपार", "evening": "संध्याकाळ", "night": "रात्र"},
        "ta": {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"},
        "en": {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "night": "night"},
    }
    return labels.get(lang, labels["en"]).get(low, time_label)


def _relative_day_confirmation(transcript: str, lang: str) -> str | None:
    rel = _relative_day_offset(transcript, lang)
    if not rel:
        return None
    offset, spoken_ref = rel
    today = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata")).date()
    target = today + timedelta(days=offset)
    resolved = _format_local_date(target, lang)
    time_label = _extract_time_label(transcript)
    localized_time = _localize_time_label(time_label, lang) if time_label else None
    if lang == "hi":
        if localized_time:
            return f"ठीक है, {spoken_ref} यानी {resolved} को {localized_time} का slot note कर लिया। क्या यह सही है?"
        return f"ठीक है, {spoken_ref} यानी {resolved} note कर लिया। उस दिन का preferred time बताइए?"
    if lang == "te":
        if localized_time:
            return f"సరే, {spoken_ref} అంటే {resolved}, {localized_time} slot note చేశాను. ఇది సరేనా?"
        return f"సరే, {spoken_ref} అంటే {resolved} note చేశాను. ఆ రోజు preferred time చెప్పండి?"
    if lang == "mr":
        if localized_time:
            return f"ठीक आहे, {spoken_ref} म्हणजे {resolved}, {localized_time} slot note केला. हे बरोबर ना?"
        return f"ठीक आहे, {spoken_ref} म्हणजे {resolved} note केला. त्या दिवसासाठी preferred time सांगा?"
    if lang == "ta":
        if localized_time:
            return f"சரி, {spoken_ref} என்றால் {resolved}, {localized_time} slot note செய்தேன். இது சரியா?"
        return f"சரி, {spoken_ref} என்றால் {resolved} note செய்தேன். அந்த நாளுக்கான preferred time சொல்லுங்கள்?"
    if localized_time:
        return f"Got it, {spoken_ref} means {resolved}, and {localized_time} slot is noted. Is that correct?"
    return f"Got it, {spoken_ref} means {resolved}. Please share your preferred time for that day."


def _extract_registration_number(text: str) -> str | None:
    norm = _normalize_registration_text(text)
    if not norm:
        return None

    searched = re.search(
        r"\b([A-Za-z]{2})\s*[- ]?\s*(\d{1,2})\s*[- ]?\s*([A-Za-z]{1,3})\s*[- ]?\s*(\d{3,4})\b",
        norm,
        flags=re.IGNORECASE,
    )
    if searched:
        state, district, series, number = searched.groups()
        formatted = _format_registration(state, district, series, number)
        if formatted:
            return formatted

    # Direct compact formats: MH12AB1234, MH 12 AB 1234, MH-12-AB-1234
    compact = re.sub(r"[^A-Za-z0-9]", "", norm).upper()
    direct = _registration_from_compact(compact)
    if direct:
        return direct

    # Spoken/mixed formats: "m h one two a b one two three four"
    tokens = [t for t in re.split(r"\s+", norm) if t]
    converted: list[str] = []
    for tok in tokens:
        plain = re.sub(r"[^\w\u0900-\u097F\u0C00-\u0C7F\u0B80-\u0BFF]", "", tok)
        plain_l = plain.lower()
        if not plain:
            continue
        if plain_l in {"oh", "o"}:
            converted.append("0")
        elif plain in _SPOKEN_LETTER_MAP:
            converted.append(_SPOKEN_LETTER_MAP[plain])
        elif plain_l in _SPOKEN_LETTER_MAP:
            converted.append(_SPOKEN_LETTER_MAP[plain_l])
        elif plain in _SPOKEN_DIGIT_MAP:
            converted.append(_SPOKEN_DIGIT_MAP[plain])
        elif plain_l in _SPOKEN_DIGIT_MAP:
            converted.append(_SPOKEN_DIGIT_MAP[plain_l])
        elif re.fullmatch(r"[a-z]", plain_l):
            converted.append(plain_l.upper())
        elif re.fullmatch(r"\d+", plain_l):
            converted.append(plain)
        elif re.fullmatch(r"[a-z]{2,3}", plain_l):
            converted.append(plain_l.upper())
        elif re.fullmatch(r"[a-z]{2}\d{1,2}[a-z]{1,3}\d{3,4}", plain_l):
            converted.append(plain_l.upper())

    merged = "".join(converted)
    return _registration_from_compact(merged)


def _registration_confirm_prompt(registration: str, lang: str) -> str:
    readback = _registration_readback(registration)
    if lang == "hi":
        return f"ठीक है, registration number {readback} note कर लिया। क्या यह सही है?"
    if lang == "te":
        return f"సరే, registration number {readback} note చేశాను. ఇది సరేనా?"
    if lang == "mr":
        return f"ठीक आहे, registration number {readback} note केला. हे बरोबर ना?"
    if lang == "ta":
        return f"சரி, registration number {readback} note செய்தேன். இது சரியா?"
    return f"Got it, registration number {readback} is noted. Is that correct?"


def _pincode_to_registration_prompt(pincode_readback: str, lang: str) -> str:
    if lang == "hi":
        return (
            f"ठीक है, आपका pincode {pincode_readback} note हो गया है। "
            "अब कृपया Montra vehicle का registration number बताइए।"
        )
    if lang == "te":
        return (
            f"సరే, మీ pincode {pincode_readback} note అయ్యింది. "
            "ఇప్పుడు దయచేసి Montra vehicle registration number చెప్పండి."
        )
    if lang == "mr":
        return (
            f"ठीक आहे, तुमचा pincode {pincode_readback} note झाला आहे. "
            "आता कृपया Montra vehicle registration number सांगा."
        )
    if lang == "ta":
        return (
            f"சரி, உங்கள் pincode {pincode_readback} note ஆகியுள்ளது. "
            "இப்போது Montra vehicle registration number சொல்லுங்கள்."
        )
    return (
        f"Got it, your pincode {pincode_readback} is noted. "
        "Please share your Montra vehicle registration number now."
    )


def _registration_confirmed_with_sr_prompt(registration: str, sr_number: str, lang: str) -> str:
    readback = _registration_readback(registration)
    if lang == "hi":
        return (
            f"ठीक है, registration number {readback} confirm हो गया। "
            f"आपका SR number SR {sr_number} है। "
            "अब कृपया service visit के लिए preferred day और time बताइए।"
        )
    if lang == "te":
        return (
            f"సరే, registration number {readback} confirm అయ్యింది. "
            f"మీ SR number SR {sr_number}. "
            "ఇప్పుడు service visit కోసం preferred day and time చెప్పండి."
        )
    if lang == "mr":
        return (
            f"ठीक आहे, registration number {readback} confirm झाला आहे. "
            f"तुमचा SR number SR {sr_number} आहे. "
            "आता service visit साठी preferred day आणि time सांगा."
        )
    if lang == "ta":
        return (
            f"சரி, registration number {readback} confirm ஆகியுள்ளது. "
            f"உங்கள் SR number SR {sr_number}. "
            "இப்போது service visitக்கான preferred day and time சொல்லுங்கள்."
        )
    return (
        f"Great, registration number {readback} is confirmed. "
        f"Your SR number is SR {sr_number}. "
        "Please share your preferred day and time for the service visit."
    )


def _slot_confirmation_prompt(slot_text: str, lang: str) -> str:
    if lang == "hi":
        return f"ठीक है, {slot_text} का slot note कर लिया। क्या यह सही है?"
    if lang == "te":
        return f"సరే, {slot_text} slot note చేశాను. ఇది సరేనా?"
    if lang == "mr":
        return f"ठीक आहे, {slot_text} slot note केला. हे बरोबर ना?"
    if lang == "ta":
        return f"சரி, {slot_text} slot note செய்தேன். இது சரியா?"
    return f"Got it, I have noted the slot as {slot_text}. Is that correct?"


def _build_step_enquiry_answer(text: str, session: CallSession, lang: str) -> str:
    """
    Give a short answer to a side enquiry and allow immediate return to
    the pending step. Uses structured session values only (deterministic).
    """
    norm = _normalize_address_text(text)
    low = norm.lower()

    if ("address" in low or "पता" in norm) and session.get("address"):
        addr = session.get("address")
        if lang == "hi":
            return f"आपका noted address है: {addr}."
        return f"Your noted address is: {addr}."

    if ("pincode" in low or "pin code" in low or "पिन" in norm) and session.get("parsed_pincode"):
        pin = session.get("parsed_pincode")
        if lang == "hi":
            return f"आपका noted pincode है: {pin}."
        return f"Your noted pincode is: {pin}."

    if ("sr" in low or "service request" in low or "टिकट" in norm or "रिक्वेस्ट" in norm):
        sr = session.get("sr_number")
        if sr:
            if lang == "hi":
                return f"आपका service request number SR {sr} है."
            return f"Your service request number is SR {sr}."

    if "detail" in low or "details" in low or "डिटेल" in norm or "जानकारी" in norm:
        if lang == "hi":
            return "सारी booking details आपको WhatsApp पर भी share की जाएंगी."
        return "All booking details will also be shared with you on WhatsApp."

    if lang == "hi":
        return "जी, आपकी query note कर ली है."
    return "Sure, I have noted your query."


# Items Godrej Appliances does NOT service (even when the user thinks Godrej
# makes them). Includes items from sister companies (Godrej Locks, Godrej
# Bullets) whose service flows through different teams — from this agent's
# POV they are out of scope. If any of these appear in the caller's utterance
# or their screener-supplied intent AND no in-scope appliance is also present,
# we inject an [OUT_OF_SCOPE_PRODUCT:<term>] marker so the LLM takes the
# graceful-refusal path instead of hallucinating a Godrej variant.
_OUT_OF_SCOPE_KEYWORDS: tuple[str, ...] = (
    # vehicles
    "car", "कार", "गाड़ी", "gaadi", "gaari",
    "bike", "बाइक", "motorcycle", "मोटरसाइकिल",
    "scooty", "scooter", "स्कूटी", "bullet", "बुलेट",
    # electronics
    "mobile", "phone", "फोन", "laptop", "लैपटॉप", "computer",
    "tv", "television", "टीवी", "tablet",
    # small / non-serviced appliances
    "iron", "इस्त्री", "istri", "press",
    "mixer", "मिक्सर", "grinder",
    "geyser", "गीज़र", "water heater",
    "kettle", "toaster",
    "induction", "इंडक्शन", "heater", "हीटर",
    "ceiling fan", "पंखा", "pankha",
    "tube light", "bulb", "बल्ब", "bulb", "lamp",
    # Godrej-branded-but-different-company
    "lock", "ताला", "tala", "taala",
    "safe", "locker",
    "almirah", "अलमारी", "almari",
    "cupboard", "wardrobe",
    # furniture / real-estate
    "sofa", "सोफा", "chair", "कुर्सी", "kursi",
    "bed", "furniture",
    "property", "flat booking", "real estate",
)

# Non-serviceable issue keywords for in-scope appliances where the issue itself
# is not a technical repair case.
_THEFT_KEYWORDS: tuple[str, ...] = (
    "stolen", "theft", "robbed", "robbery", "looted", "loot", "snatched",
    "chori", "chori ho", "chura", "चोरी", "लूट", "चुरा",
)

_PEST_DANGER_KEYWORDS: tuple[str, ...] = (
    "mouse", "rat", "rodent", "snake", "lizard", "cockroach",
    "chuha", "chooha", "chuhiya", "chuhia", "chipkali",
    "saap", "saamp", "सांप", "साँप", "चूहा", "चुहा", "चूहे", "चुहे",
    "छिपकली", "कॉकरोच",
)

# Products the service agent IS trained to handle. When an in-scope appliance
# is mentioned in the same utterance as an OOS term, we assume the complaint
# is about the in-scope item (e.g., "AC ke paas gaadi khadi thi" — the caller
# is complaining about an AC and incidentally mentioned a car).
_IN_SCOPE_KEYWORDS: tuple[str, ...] = (
    "washing machine", "वॉशिंग मशीन", "washer", "मशीन",
    "ac", "एसी", "air conditioner", "एयर कंडीशनर",
    "air cooler", "cooler", "कूलर",
    "refrigerator", "fridge", "फ्रिज", "रेफ्रिजरेटर",
    "freezer", "डीप फ्रीजर", "deep freezer",
    "microwave", "माइक्रोवेव", "oven", "ओवन",
    "dishwasher", "डिशवॉशर",
    "insulicool",
)


def _detect_out_of_scope_product(text: str) -> str | None:
    low = text.lower()
    oos_hit = next((kw for kw in _OUT_OF_SCOPE_KEYWORDS if kw in low), None)
    if not oos_hit:
        return None
    if any(kw in low for kw in _IN_SCOPE_KEYWORDS):
        return None
    return oos_hit


def _detect_non_service_issue(text: str) -> str | None:
    norm = _normalize_address_text(text)
    if not norm:
        return None
    low = norm.lower()

    # Avoid false positives on explicit negations.
    if "चोरी नहीं" not in norm and "not stolen" not in low and any(
        kw in low for kw in _THEFT_KEYWORDS
    ):
        return "theft"
    if any(kw in low for kw in _PEST_DANGER_KEYWORDS):
        return "pest_or_animal"
    return None


def _non_service_refusal_text(lang: str, issue_type: str) -> str:
    if lang == "hi":
        if issue_type == "theft":
            return (
                "माफ़ कीजिएगा, इसमें मैं आपकी मदद नहीं कर पाऊँगी। "
                "चोरी के मामले में कृपया पुलिस की मदद लें। "
                "मैं सिर्फ Godrej Appliances की technical service booking के लिए responsible हूँ।"
            )
        return (
            "माफ़ कीजिएगा, मैं इस चीज़ में मदद नहीं कर पाऊँगी। "
            "मैं सिर्फ Godrej Appliances की technical service booking के लिए responsible हूँ।"
        )
    if issue_type == "theft":
        return (
            "I am sorry, I cannot help with this. For theft-related issues, "
            "please contact the police. I am only responsible for Godrej Appliances technical service bookings."
        )
    return (
        "I am sorry, I cannot help with this. I am only responsible for "
        "Godrej Appliances technical service bookings."
    )


class ServiceAgent(BaseAgent):
    name = "service"
    can_handoff_to = ["scheduler", "sales", "closer"]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_responses = deque(maxlen=5)  # Track last 5 responses
        self.pace_profiler   = CallerPaceProfiler()  # NEW

    def _preprocess_transcript(self, transcript: str, session) -> str:
        # Outbound NPS surveys have no service requests — skip all inbound
        # Godrej preprocessing (SR generation, pincode parsing, OOS detection)
        # to prevent hallucinated SR numbers leaking into the LLM context.
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            return transcript

        sr_number = session.get("sr_number")
        if not sr_number:
            sr_number = str(random.randint(1000, 9999))
            session.set("sr_number", sr_number)

        issue = session.get("intent", "") or session.get("reported_issue", "")
        language = session.get("language", session.current_language)
        support_domain = (session.get("support_domain") or "appliances").strip().lower()
        non_service_issue = None if support_domain == "automotive" else _detect_non_service_issue(f"{transcript} {issue}")

        if non_service_issue:
            session.set("non_service_issue_type", non_service_issue)
            logger.info(
                f"[service] Non-service issue detected: '{non_service_issue}' → "
                f"injecting strict refusal context"
            )
            return "\n".join([
                "[INTERNAL CONTEXT: NON_SERVICE_ISSUE_DETECTED. "
                "This issue is outside technical service booking scope "
                "(e.g., theft/pest/animal situation). Respond with strict refusal: "
                "\"माफ़ कीजिएगा, मैं इस चीज़ में मदद नहीं कर पाऊँगी। मैं सिर्फ Godrej Appliances की technical service booking के लिए responsible हूँ।\" "
                "Do NOT ask diagnostics. Do NOT offer transfer. Emit [END_CALL] silently.]",
                f"Customer said: {transcript}",
            ])

        # Out-of-scope product detection — runs before anything that could push
        # the LLM toward troubleshooting. Once address/pincode has been captured
        # we stop checking so e.g. "mera address car park के पास है" doesn't
        # flip an already-booked complaint into OOS.
        oos_term: str | None = None
        if support_domain != "automotive" and not session.get("address") and not session.get("product"):
            oos_term = _detect_out_of_scope_product(f"{transcript} {issue}")
            if oos_term:
                session.set("out_of_scope_product", oos_term)
                logger.info(
                    f"[service] OOS detected: '{oos_term}' in transcript/intent → "
                    f"injecting [OUT_OF_SCOPE_PRODUCT]"
                )

        registration_like = _looks_like_registration_value(transcript)
        parsed_pin_value = (session.get("parsed_pincode") or "").strip()
        parsed_pin_already = bool(parsed_pin_value)
        # If pincode is already captured and caller is dictating vehicle registration,
        # never re-parse this turn as pincode.
        skip_pincode_parse = parsed_pin_already and registration_like
        pincode = NumericParseResult(digits=None, readback=None, invalid_candidate=None)
        if not skip_pincode_parse:
            pincode = parse_indian_pincode(transcript, lang=language)

        if pincode.digits:
            session.set("parsed_pincode", pincode.digits)
            if pincode.readback:
                session.set("parsed_pincode_readback", pincode.readback)
        elif pincode.invalid_candidate:
            session.set("invalid_pincode_candidate", pincode.invalid_candidate)
        elif skip_pincode_parse:
            # Prevent stale invalid-pin hints when caller is dictating vehicle registration.
            session.set("invalid_pincode_candidate", "")

        # When OOS is detected, skip the usual SR / pincode / "go directly to
        # Step 2" scaffolding — the LLM must take the refusal path.
        if oos_term:
            return "\n".join([
                f"[INTERNAL CONTEXT: OUT_OF_SCOPE_PRODUCT:{oos_term}. "
                f"Godrej Appliances does NOT service \"{oos_term}\". "
                f"Follow the OUT-OF-SCOPE path — do NOT ask any diagnostic or "
                f"troubleshooting questions (no power, engine, cooling, drum, "
                f"compressor, warranty, SR, slot, address). Do NOT assume a "
                f"Godrej variant of this product exists. Name and mobile are "
                f"already captured — go directly to the gentle redirect line "
                f"(Step C) and then emit [END_CALL].]",
                f"Customer said: {transcript}",
            ])

        if not issue:
            extra = [f"[INTERNAL CONTEXT: Pre-generated SR Number: {sr_number}. USE THIS EXACT NUMBER in Step 3.]"]
            if pincode.digits:
                extra.append(f"[PARSED_PINCODE:{pincode.digits}]")
                if pincode.readback:
                    extra.append(f"[PINCODE_READBACK:{pincode.readback}]")
            elif parsed_pin_value:
                extra.append(f"[PARSED_PINCODE:{parsed_pin_value}]")
            elif pincode.invalid_candidate:
                extra.append(f"[INVALID_PINCODE:{pincode.invalid_candidate}]")
            if parsed_pin_value and registration_like:
                extra.append(
                    "[INTERNAL CONTEXT: Current customer utterance looks like vehicle registration details. "
                    "Do NOT ask pincode again. Continue with registration capture/confirmation and proceed in service flow.]"
                )
            return "\n".join([transcript, *extra])

        ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        current_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p")

        lines = [
            "[INTERNAL CONTEXT — already captured earlier in the call]",
            f"Current Date and Time (IST): {current_time}",
            f"Customer name: {session.get('name', '')}" if session.get("name") else "",
            f"Customer mobile: {session.get('mobile', '')}" if session.get("mobile") else "",
            f"Session language: {language}",
            f"Pre-generated SR Number: {sr_number}. USE THIS EXACT NUMBER in Step 3. NEVER change it.",
        ]
        if issue:
            lines.append(f"CRITICAL KNOWN ISSUE: {issue}. DO NOT ask what the appliance/issue is. Acknowledge this issue and go DIRECTLY to Step 2 (Troubleshooting).")
        if pincode.digits:
            lines.append(f"Structured pincode already captured: {pincode.digits}")
            if pincode.readback:
                lines.append(f"Speak this pincode back naturally as: {pincode.readback}")
        elif parsed_pin_value:
            lines.append(f"Structured pincode already captured: {parsed_pin_value}")
        elif pincode.invalid_candidate:
            lines.append(
                f"Invalid pincode candidate captured: {pincode.invalid_candidate}. "
                "Do NOT accept it. Ask again for exactly 6 digits."
            )
        if parsed_pin_value and registration_like:
            lines.append(
                "The current customer utterance appears to be a vehicle registration number. "
                "Do NOT ask for pincode again. Continue with registration capture/confirmation."
            )
        if _is_short_confirmation(transcript):
            lines.append(
                "The current customer utterance is only a confirmation. "
                "Do NOT ask the issue again. Start directly from service troubleshooting."
            )

        # Add specific instruction for address and pincode confirmation
        current_address = session.get("address")
        current_pincode = session.get("parsed_pincode")
        if current_address and current_pincode:
            lines.append(
                f"CRITICAL: The customer's address '{current_address}' and pincode '{current_pincode}' "
                "have been collected. You MUST confirm BOTH the full address AND the pincode "
                "together, exactly as specified in STEP 5 of the workflow. "
                "Example: 'Confirm कर रही हूँ — [full address], [pincode] — क्या यह सही है?'"
            )

        lines.append(
            "CRITICAL WORKFLOW ENFORCEMENT: You MUST strictly follow the step-by-step workflow in exact order. "
            "Never skip the slot booking (date and time), address, or pincode steps. Do NOT merge steps inappropriately."
        )
        
        pending_step = (session.get("service_pending_step") or "").strip()
        if pending_step:
            lines.append(f"STRICT STEP LOCK: We are currently on the '{pending_step}' step. You MUST ask for or confirm the {pending_step} in your response.")
            
        lines.append("[END INTERNAL CONTEXT]")
        lines.append(f"Customer said: {transcript}")
        return "\n".join(line for line in lines if line)

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        # Outbound calls: skip all inbound appliance-specific preprocessing entirely
        if session.get("direction") == "outbound" or session.get("support_domain") == "outbound":
            reply = await self._chat(session, transcript)
            handoff = self._parse_handoff(reply)
            end_call = "[END_CALL]" in reply
            clean_reply = re.sub(
                r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|PARSED_MOBILE|INTENT|MCP|TOOL):[^\]]*\]|\[END_CALL\]",
                "", reply,
            ).strip()
            if handoff:
                return AgentResponse(text=clean_reply, handoff=HandoffSignal(target=handoff))
            if end_call:
                return AgentResponse(text=clean_reply, end_call=True)
            return AgentResponse(text=clean_reply)

        # NEW ── record transcript into pace profiler before LLM call
        self.pace_profiler.record_transcript(transcript)
        current_pace = self.pace_profiler.current_pace
        logger.debug(
            f"[PACE] {current_pace.value} | avg_wps={self.pace_profiler.avg_wps:.2f} | "
            f"tts_rate={self.pace_profiler.tts_rate_multiplier:.2f} | "
            f"ep_ms={self.pace_profiler.endpointing_ms}"
        )

        lang = session.get("language", session.current_language) or "hi"
        issue = session.get("intent", "") or session.get("reported_issue", "")
        support_domain = (session.get("support_domain") or "appliances").strip().lower()
        non_service_issue = None if support_domain == "automotive" else _detect_non_service_issue(f"{transcript} {issue}")
        if non_service_issue:
            refusal = _non_service_refusal_text(lang, non_service_issue)
            session.set("non_service_issue_type", non_service_issue)
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
            session.add_message("user", transcript)
            session.add_message("assistant", refusal)
            logger.info(
                f"[SERVICE STRICT REFUSAL] non_service_issue={non_service_issue} "
                "→ deterministic end_call"
            )
            return AgentResponse(text=refusal, end_call=True)

        existing_address = (session.get("address") or "").strip()
        expecting_address = bool(session.get("expecting_address"))
        pending_step = (session.get("service_pending_step") or "").strip().lower()
        pending_prompt = (session.get("service_pending_prompt") or "").strip()
        pincode_probe = parse_indian_pincode(transcript, lang=lang)
        registration_like = _looks_like_registration_value(transcript)
        automotive_flow = support_domain == "automotive"
        parsed_registration = (session.get("parsed_registration") or "").strip()
        registration_candidate = _extract_registration_number(transcript) if automotive_flow else None
        if automotive_flow and registration_candidate:
            session.set("parsed_registration", registration_candidate)
            parsed_registration = registration_candidate
        if pincode_probe.digits:
            session.set("parsed_pincode", pincode_probe.digits)
            if pincode_probe.readback:
                session.set("parsed_pincode_readback", pincode_probe.readback)
        elif pincode_probe.invalid_candidate:
            session.set("invalid_pincode_candidate", pincode_probe.invalid_candidate)
        if session.get("parsed_pincode") and registration_like and pending_step == "pincode":
            # Caller has moved to registration input; unlock from pincode step.
            pending_step = ""
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
            pincode_probe = NumericParseResult(digits=None, readback=None, invalid_candidate=None)

        has_negative_correction = _looks_like_negative_correction(transcript)
        address_like_value = _looks_like_address_value(transcript)
        enquiry_turn = _is_enquiry_turn(transcript)

        if automotive_flow and pending_step == "registration_confirm":
            if _is_short_confirmation(transcript):
                if not parsed_registration:
                    retry = _registration_followup_prompt(lang)
                    session.set("service_pending_step", "registration")
                    session.set("service_pending_prompt", retry)
                    session.add_message("user", transcript)
                    session.add_message("assistant", retry)
                    return AgentResponse(text=retry)
                sr_number = str(session.get("sr_number") or random.randint(1000, 9999))
                session.set("sr_number", sr_number)
                if parsed_registration:
                    session.set("registration_confirmed", True)
                session.set("service_pending_step", "slot")
                session.set("service_pending_prompt", _post_sr_slot_prompt(lang))
                deterministic = _registration_confirmed_with_sr_prompt(parsed_registration, sr_number, lang)
                session.add_message("user", transcript)
                session.add_message("assistant", deterministic)
                return AgentResponse(text=deterministic)
            if _is_short_negative(transcript):
                session.set("parsed_registration", "")
                session.set("registration_confirmed", False)
                session.set("service_pending_step", "registration")
                session.set("service_pending_prompt", _registration_followup_prompt(lang))
                retry = _registration_followup_prompt(lang)
                session.add_message("user", transcript)
                session.add_message("assistant", retry)
                return AgentResponse(text=retry)
            if registration_candidate:
                session.set("service_pending_step", "registration_confirm")
                session.set("service_pending_prompt", _registration_confirm_prompt(registration_candidate, lang))
                deterministic = _registration_confirm_prompt(registration_candidate, lang)
                session.add_message("user", transcript)
                session.add_message("assistant", deterministic)
                return AgentResponse(text=deterministic)

        if automotive_flow and session.get("parsed_pincode") and registration_candidate:
            session.set("service_pending_step", "registration_confirm")
            session.set("service_pending_prompt", _registration_confirm_prompt(registration_candidate, lang))
            deterministic = _registration_confirm_prompt(registration_candidate, lang)
            session.add_message("user", transcript)
            session.add_message("assistant", deterministic)
            return AgentResponse(text=deterministic)

        if automotive_flow and pincode_probe.digits and not parsed_registration:
            readback = pincode_probe.readback or " ".join(list(pincode_probe.digits))
            session.set("service_pending_step", "registration")
            session.set("service_pending_prompt", _registration_followup_prompt(lang))
            deterministic = _pincode_to_registration_prompt(readback, lang)
            session.add_message("user", transcript)
            session.add_message("assistant", deterministic)
            return AgentResponse(text=deterministic)

        if automotive_flow and pending_step == "slot":
            relative_reply = _relative_day_confirmation(transcript, lang)
            if relative_reply:
                session.set("preferred_slot_text", _normalize_address_text(transcript))
                if _extract_time_label(transcript):
                    session.set("service_pending_step", "")
                    session.set("service_pending_prompt", "")
                    session.set("slot_confirmed", True)
                else:
                    session.set("service_pending_step", "slot")
                    session.set("service_pending_prompt", _default_pending_prompt("slot", lang))
                session.add_message("user", transcript)
                session.add_message("assistant", relative_reply)
                return AgentResponse(text=relative_reply)
            if _looks_like_slot_value(transcript):
                slot_text = _normalize_address_text(transcript)
                session.set("preferred_slot_text", slot_text)
                session.set("service_pending_step", "")
                session.set("service_pending_prompt", "")
                session.set("slot_confirmed", True)
                deterministic = _slot_confirmation_prompt(slot_text, lang)
                session.add_message("user", transcript)
                session.add_message("assistant", deterministic)
                return AgentResponse(text=deterministic)

        # If the pending field is answered in the current turn, clear step-lock.
        if pending_step == "address" and address_like_value:
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
        elif pending_step == "slot" and _looks_like_slot_value(transcript):
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
        elif pending_step == "pincode" and pincode_probe.digits:
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")

        # Step-lock guard: if user asks a side-question while a required field
        # is still pending, answer briefly and return to the exact pending step.
        if pending_step and enquiry_turn:
            asked_pending_value = (
                (pending_step == "slot" and _looks_like_slot_value(transcript))
                or
                (pending_step == "address" and address_like_value)
                or (pending_step == "pincode" and bool(pincode_probe.digits))
            )
            if not asked_pending_value:
                side_answer = _build_step_enquiry_answer(transcript, session, lang)
                reask = pending_prompt or _default_pending_prompt(pending_step, lang)
                combined = f"{side_answer} {reask}".strip()
                session.add_message("user", transcript)
                session.add_message("assistant", combined)
                logger.info(
                    f"[STEP LOCK] pending_step={pending_step} enquiry handled; returning to pending step"
                )
                return AgentResponse(text=combined)

        # Address correction guard:
        # If we already have an address but caller says a short negative correction
        # (e.g., "नहीं नहीं अशोक"), clear stale address and re-ask full address.
        # This prevents repeatedly confirming a truncated tail.
        if (
            existing_address
            and not pincode_probe.digits
            and has_negative_correction
            and not address_like_value
        ):
            session.set("address", "")
            session.set("expecting_address", True)
            session.set("service_pending_step", "address")
            session.set("service_pending_prompt", _default_pending_prompt("address", lang))
            correction_prompt = (
                "क्षमा कीजिए, सही capture के लिए कृपया अपना पूरा address एक बार फिर बताइए?"
                if lang == "hi"
                else "Sorry about that. To capture it correctly, could you please share your full address once again?"
            )
            session.add_message("user", transcript)
            session.add_message("assistant", correction_prompt)
            logger.info("[ADDRESS CORRECTION] Cleared stale address; re-asking full address")
            return AgentResponse(text=correction_prompt)

        # Deterministic address capture:
        # In address-capture phase, store the entire utterance as the address
        # (if it looks like an address value) to avoid LLM truncation.
        if (
            not pincode_probe.digits
            and address_like_value
            and (expecting_address or (existing_address and has_negative_correction))
        ):
            full_address = _normalize_address_text(transcript)
            session.set("address", full_address)
            session.set("expecting_address", False)
            session.set("service_pending_step", "pincode")
            session.set("service_pending_prompt", _default_pending_prompt("pincode", lang))
            deterministic = (
                f"धन्यवाद, आपका पूरा address note कर लिया: {full_address}। अब आपका 6 अंकों का pincode क्या है?"
                if lang == "hi"
                else f"Thank you, I have noted your full address: {full_address}. What is your 6-digit pincode?"
            )
            session.add_message("user", transcript)
            session.add_message("assistant", deterministic)
            logger.info(f"[DETERMINISTIC ADDRESS CAPTURE] '{full_address}'")
            return AgentResponse(text=deterministic)

        reply = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply

        # Capture address when LLM emits the tag
        address_match = re.search(r"\[ADDRESS:([^\]]+)\]", reply)
        if address_match:
            address = address_match.group(1).strip()

            # Safety net — the LLM frequently truncates long addresses, keeping
            # only the tail ("Vaishali Nagar, Jaipur") when the transcript held
            # the full string ("Flat 107, Aravali homes, Gandhi path, Vaishali
            # Nagar, Jaipur"). If the raw transcript is meaningfully longer AND
            # ends with the LLM's extraction, trust the raw transcript.
            raw = (transcript or "").strip()
            raw_normalized = re.sub(r"\s+", " ", raw).strip(" .,")
            suffix_similarity = 0.0
            if raw_normalized and address:
                raw_tail = raw_normalized[-len(address):]
                suffix_similarity = difflib.SequenceMatcher(
                    None, raw_tail.lower(), address.lower()
                ).ratio()
            if (
                raw_normalized
                and len(raw_normalized) >= len(address) + 8
                and (
                    raw_normalized.lower().endswith(address.lower())
                    or suffix_similarity >= 0.78
                )
            ):
                logger.info(
                    f"[ADDRESS SAFETY NET] LLM truncated '{address}' → using raw "
                    f"transcript '{raw_normalized}' (suffix_similarity={suffix_similarity:.2f})"
                )
                address = raw_normalized

            had_address_before = bool(session.get("address"))
            session.set("address", address)
            session.set("expecting_address", False)
            session.set("service_pending_step", "pincode")
            session.set("service_pending_prompt", _default_pending_prompt("pincode", lang))

            # Deterministic address readback — bypass LLM's truncated reply.
            # Same pattern as mobile number confirmation in HelloAgent: the LLM
            # extracts the address into the tag, but its spoken reply often drops
            # the first half due to the 90-150 token budget.  We echo the full
            # address from the tag and ask for pincode, all in code.
            if not had_address_before and not handoff and not end_call:
                lang = session.get("language", session.current_language) or "hi"
                if lang == "hi":
                    deterministic = f"{address} — note कर लिया। और आपके area का 6 अंकों का pincode क्या है?"
                else:
                    deterministic = f"Noted — {address}. And what is your 6-digit pincode?"
                session.add_message("user", transcript)
                session.add_message("assistant", deterministic)
                logger.info(f"[DETERMINISTIC ADDRESS READBACK] '{address}' (lang={lang})")
                return AgentResponse(text=deterministic)
        elif _is_address_ask(reply) and not session.get("address"):
            # LLM just asked for address — extend the pipeline's utterance hold
            # so the next user turn captures the full multi-part address.
            session.set("expecting_address", True)
            session.set("service_pending_step", "address")
            session.set("service_pending_prompt", _default_pending_prompt("address", lang))
            logger.info("[ADDRESS ASK] expecting_address=True — pipeline will extend hold")
        elif _is_pincode_ask(reply) and not pincode_probe.digits:
            if session.get("parsed_pincode") and registration_like:
                # LLM drift guard: avoid re-locking on pincode after it was already captured.
                session.set("service_pending_step", "")
                session.set("service_pending_prompt", "")
            else:
                session.set("service_pending_step", "pincode")
                session.set("service_pending_prompt", _default_pending_prompt("pincode", lang))
        elif _is_slot_ask(reply):
            session.set("service_pending_step", "slot")
            session.set("service_pending_prompt", _default_pending_prompt("slot", lang))
            if automotive_flow:
                session.set("service_slot_asked", True)

        clean_reply = re.sub(
            r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP|ADDRESS):[^\]]*\]|\[END_CALL\]",
            "", reply,
        ).strip()
        if session.get("parsed_pincode") and registration_like and _is_pincode_ask(clean_reply):
            # Do not speak another pincode ask when caller is already sharing registration.
            clean_reply = _registration_followup_prompt(lang)

        if (
            automotive_flow
            and session.get("registration_confirmed")
            and not session.get("service_slot_asked")
        ):
            sr_number = str(session.get("sr_number") or "")
            low_clean_reply = clean_reply.lower()
            sr_spoken = ("sr" in low_clean_reply) or (sr_number and sr_number in clean_reply)
            if sr_spoken and not _is_slot_ask(clean_reply):
                slot_followup = _post_sr_slot_prompt(lang)
                clean_reply = f"{clean_reply} {slot_followup}".strip()
                session.set("service_pending_step", "slot")
                session.set("service_pending_prompt", _default_pending_prompt("slot", lang))
                session.set("service_slot_asked", True)

        # NEW ── attach pace metadata to session so StreamingPipeline can read it for TTS
        session.set("caller_pace",        current_pace.value)
        session.set("tts_rate_multiplier", self.pace_profiler.tts_rate_multiplier)
        session.set("tts_ssml_rate",       self.pace_profiler.ssml_rate)

        # ── Loop detection: if same message repeated 2+ times, force handoff to closer ──
        if clean_reply and not handoff and not end_call:
            self._last_responses.append(clean_reply)
            
            # Check for loops: any 2 consecutive responses with >50% similarity = loop
            if len(self._last_responses) >= 2:
                last_reply = self._last_responses[-1]
                prev_reply = self._last_responses[-2]
                similarity = _message_similarity(last_reply, prev_reply)
                
                if similarity > 0.50:  # Lowered threshold to catch more loops
                    logger.warning(
                        f"[LOOP DETECTED] Similarity={similarity:.2f} (threshold=0.50). "
                        f"Last: '{last_reply[:60]}...' | Prev: '{prev_reply[:60]}...' → "
                        f"Force handoff to closer"
                    )
                    # Looping detected — force handoff to closer
                    return AgentResponse(
                        text="",
                        handoff=HandoffSignal(
                            target="closer",
                            data={
                                "customer_name":   session.get("customer_name", ""),
                                "customer_mobile": session.get("customer_mobile", ""),
                                "language":        session.get("language", ""),
                                "product":         session.get("product", ""),
                                "address":         session.get("address", ""),
                            },
                        ),
                    )
            else:
                logger.debug(f"[SERVICE] Response tracked (total: {len(self._last_responses)})")


        if handoff:
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
            # Enforce silent handoff at code-level: drop transition phrases even
            # if prompt-following drifts in multilingual calls.
            spoken = _strip_handoff_transition_text(clean_reply)
            return AgentResponse(
                text=spoken,
                handoff=HandoffSignal(
                    target=handoff,
                    data={
                        "customer_name":   session.get("customer_name", ""),
                        "customer_mobile": session.get("customer_mobile", ""),
                        "language":        session.get("language", ""),
                        "product":         session.get("product", ""),
                        "address":         session.get("address", ""),
                    },
                ),
            )
        if end_call:
            session.set("service_pending_step", "")
            session.set("service_pending_prompt", "")
            return AgentResponse(text=clean_reply, end_call=True)
        return AgentResponse(text=clean_reply)

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
