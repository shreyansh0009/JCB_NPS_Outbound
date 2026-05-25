"""
LanguageDetector — detects Indian languages from transcript text.

Supported languages:
    hi  — Hindi
    bn  — Bengali
    te  — Telugu
    mr  — Marathi
    ta  — Tamil
    gu  — Gujarati
    kn  — Kannada
    pa  — Punjabi
    ml  — Malayalam
    or  — Odia
    en  — English (default / fallback)

Detection strategy:
    1. Explicit request phrases ("let's talk in Hindi", "hindi mein baat karo")
       → immediate switch, highest priority
    2. Unicode script matching (Devanagari, Tamil, Telugu, etc.)
       → immediate switch, unambiguous
    3. Hinglish keyword matching (Roman-script Hindi words like "namaste",
       "haan", "nahi", "kya", "aap", "mein") — catches spoken Hindi that
       Deepgram transcribes in Roman letters before the swap completes
       → immediate switch
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# -- English greetings to ignore for language switching in non-English sessions
_ENGLISH_GREETINGS = {"hello", "hi", "hey"}

# ── Unicode script → language ─────────────────────────────────────────────────
_SCRIPT_RANGES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[\u0900-\u097F]"), "hi"),   # Devanagari (Hindi/Marathi)
    (re.compile(r"[\u0980-\u09FF]"), "bn"),   # Bengali
    (re.compile(r"[\u0C00-\u0C7F]"), "te"),   # Telugu
    (re.compile(r"[\u0B80-\u0BFF]"), "ta"),   # Tamil
    (re.compile(r"[\u0A80-\u0AFF]"), "gu"),   # Gujarati
    (re.compile(r"[\u0C80-\u0CFF]"), "kn"),   # Kannada
    (re.compile(r"[\u0A00-\u0A7F]"), "pa"),   # Gurmukhi (Punjabi)
    (re.compile(r"[\u0D00-\u0D7F]"), "ml"),   # Malayalam
    (re.compile(r"[\u0B00-\u0B7F]"), "or"),   # Odia
]

# Marathi-specific Devanagari keywords
_MARATHI_KEYWORDS = re.compile(
    r"\b(आहे|नाही|माझ्या|तुमच्या|मला|तुम्हाला|आपण|होय)\b"
)

# ── Explicit language-switch request detection ────────────────────────────────
# Catches: "can we talk in hindi", "speak Tamil", "hindi mein baat karo",
# "क्या हम मराठी में बात कर सकते हैं", "மराठीत बोलू", etc.
#
# Implementation note: we do NOT use Python's `\b` word boundary here. Devanagari
# combining marks (ा ि ी ु े ो etc., Unicode category Mc) are NOT word characters
# in Python's regex engine, so `\bमराठी\b` fails when the word ends in a matra
# (which is most of the time). Substring matching with category-based boundary
# checks handles all 5 Indian scripts reliably.
import unicodedata as _ud

_LANG_NAME_TOKENS: dict[str, str] = {
    # Latin
    "hindi": "hi", "bengali": "bn", "bangla": "bn", "telugu": "te",
    "marathi": "mr", "tamil": "ta", "gujarati": "gu", "kannada": "kn",
    "punjabi": "pa", "malayalam": "ml", "odia": "or", "odiya": "or",
    "english": "en",
    # Native scripts
    "हिंदी": "hi",   "हिन्दी": "hi",
    "बांग्ला": "bn",  "বাংলা": "bn",
    "तेलुगु": "te",  "तेलुगू": "te",  "తెలుగు": "te",
    "मराठी": "mr",
    "तमिल": "ta",   "तामिल": "ta",   "தமிழ்": "ta",
    "गुजराती": "gu", "ગુજરાતી": "gu",
    "कन्नड": "kn",  "कन्नड़": "kn",  "ಕನ್ನಡ": "kn",
    "पंजाबी": "pa",  "ਪੰਜਾਬੀ": "pa",
    "मलयालम": "ml", "മലയാളം": "ml",
    "ओड़िया": "or",  "उड़िया": "or",  "ଓଡ଼ିଆ": "or",
    "अंग्रेजी": "en", "इंग्लिश": "en", "इंग्रजी": "en",
}

# Pre-sort by length descending so longer tokens (e.g. "हिन्दी") match before
# shorter overlapping ones — minor protection against substring collisions.
_LANG_NAME_TOKENS_SORTED: list[tuple[str, str]] = sorted(
    _LANG_NAME_TOKENS.items(), key=lambda kv: -len(kv[0])
)


def _is_letter_or_mark(c: str) -> bool:
    """True if char is a letter (L*) or combining mark (M*) — i.e. part of a word."""
    if not c:
        return False
    cat = _ud.category(c)
    return cat.startswith("L") or cat.startswith("M")


# Kept for backward compat (other modules may import _NAME_TO_CODE)
_NAME_TO_CODE = _LANG_NAME_TOKENS

# ── Hinglish (Roman-script Hindi) keyword detection ───────────────────────────
# When Deepgram transcribes spoken Hindi BEFORE the connection swap,
# it returns Roman transliteration. These words are unmistakably Hindi.
# Threshold: 2+ matches in a single utterance = Hindi.
# Only truly unambiguous Hindi-only words that cannot appear in English sentences.
# Removed: please, main, se, par, ko, ka, ki, ke, ye, wo — all valid English words.
_HINGLISH_WORDS = re.compile(
    r"\b("
    r"namaste|namaskar|"
    r"haan|nahi|nahin|"
    r"kya|kaisa|kaise|kyun|kyon|"
    r"aap|tum|hum|"
    r"mein|hai|hain|"
    r"aur|lekin|"
    r"yeh|woh|wah|"
    r"karo|karna|karein|karte|"
    r"baat|bolna|bolo|suno|"
    r"theek|thik|accha|acha|bilkul|"
    r"bahut|bohot|zyada|"
    r"mujhe|mera|meri|mere|"
    r"aapka|aapki|tumhara|"
    r"shukriya|dhanyawad|"
    r"paise|rupaye|rupaya|"
    r"gaadi|gadi|"
    r"batao|bataiye|samjho"
    r")\b",
    re.IGNORECASE,
)
_HINGLISH_THRESHOLD = 2   # two unambiguous Hindi words = switch


SUPPORTED_LANGUAGES: set[str] = {"hi", "bn", "te", "mr", "ta", "gu", "kn", "pa", "ml", "or", "en"}

# ── Data-input detection (skip language switching for reg numbers, phones) ────
_DATA_INPUT_RE = re.compile(r'^[\dA-Za-z\s\.\-\/]+$')

# ── Sprint 2: ML language detection (langdetect) ──────────────────────────────
# Used as Priority 3.5: when script detection returns "en" but the text is
# >= MIN_ML_CHARS characters, ask langdetect for a second opinion.
# langdetect uses Google's language-detection library (Naive Bayes + n-gram).
# Accuracy on short sentences (< 20 chars) is low — we only use it for longer text.
#
# Install: pip install langdetect
try:
    from langdetect import detect as _ml_detect, LangDetectException as _LangDetectException
    _ML_DETECT_AVAILABLE = True
    logger.debug("langdetect available — ML language detection enabled")
except ImportError:
    _ML_DETECT_AVAILABLE = False
    logger.debug("langdetect not installed — using rule-based detection only")

# Map langdetect output codes → our codes
_ML_CODE_MAP: dict[str, str] = {
    "hi": "hi",
    "bn": "bn",
    "te": "te",
    "mr": "mr",
    "ta": "ta",
    "gu": "gu",
    "kn": "kn",
    "pa": "pa",
    "ml": "ml",
    "or": "or",
}
_MIN_ML_CHARS = 15   # only run ML detection on text longer than this (short text is unreliable)


def _is_data_input(text: str) -> bool:
    """True if text looks like a vehicle registration or phone number (language-neutral)."""
    clean = text.strip().rstrip('.')
    no_space = clean.replace(' ', '').replace('-', '').replace('/', '').replace(':', '')
    # Purely numeric (phone numbers)
    if no_space.isdigit() and len(no_space) >= 3:
        return True
    # Short alphanumeric with at least one digit (reg numbers like RJ01CA, MH14AB1234)
    if len(no_space) <= 15 and no_space.isalnum() and any(c.isdigit() for c in no_space):
        return True
    return False


def detect_explicit_language_request(text: str) -> Optional[str]:
    """
    Returns a language code if the user mentioned a language name in a way that
    looks like a switch request.

    Strategy: substring match each known language name with category-based
    boundary checks. Devanagari matras (Mc) trip up `\\b`, so we use
    `_is_letter_or_mark()` on the surrounding characters instead.

    Permissive on purpose — a customer mid-call who says "मराठी" or "tamil"
    almost always means a switch. False positives in the wild are rare; false
    negatives (the previous regex's failure mode) are user-visible.
    """
    if not text:
        return None
    text_lower = text.lower()
    for token, code in _LANG_NAME_TOKENS_SORTED:
        # Latin tokens compare case-insensitively; non-Latin compare as-is.
        is_ascii = token.isascii()
        haystack = text_lower if is_ascii else text
        needle = token.lower() if is_ascii else token

        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx < 0:
                break
            before = text[idx - 1] if idx > 0 else ""
            # Require a clean LEADING boundary (start of string or non-letter
            # before the token). The TRAILING side is permissive so we still
            # detect inflected forms like Marathi locative "मराठीत" / "हिंदीत".
            # False positives from random words containing "english" / "tamil"
            # / "मराठी" as substrings are rare in practice and acceptable.
            if not _is_letter_or_mark(before):
                logger.debug(f"Explicit language request: '{token}' → {code}")
                return code
            start = idx + 1
    return None


def _ml_detect_language(text: str) -> Optional[str]:
    """
    Sprint 2: ML-based language detection using langdetect (Google n-gram model).
    Returns a language code if detected with confidence, or None.

    Only called for longer Latin-script text when script/keyword detection returns "en"
    but we want to verify it isn't actually an Indian language (Tamil in Latin script,
    Hinglish below keyword threshold, etc.).
    """
    if not _ML_DETECT_AVAILABLE or len(text.strip()) < _MIN_ML_CHARS:
        return None
    try:
        detected = _ml_detect(text)
        mapped = _ML_CODE_MAP.get(detected)
        if mapped:
            logger.debug(f"ML language detection: '{text[:40]}' → {detected} (mapped={mapped})")
        return mapped
    except Exception:
        return None


def detect_language(text: str) -> str:
    """
    Detect language of text. Returns a language code or 'en'.
    Order: script check → Hinglish keyword check → ML detection → fallback English.
    """
    if not text or not text.strip():
        return "en"

    # 1. Unicode script (fastest, most reliable)
    for pattern, lang_code in _SCRIPT_RANGES:
        if pattern.search(text):
            if lang_code == "hi" and _MARATHI_KEYWORDS.search(text):
                return "mr"
            return lang_code

    # 2. Hinglish — Roman-script Hindi words
    matches = _HINGLISH_WORDS.findall(text.lower())
    if len(matches) >= _HINGLISH_THRESHOLD:
        logger.debug(f"Hinglish detected ({len(matches)} keywords): {matches[:5]}")
        return "hi"

    # 3. Sprint 2: ML detection for longer Latin-script text
    # Catches cases like: partial Hinglish below keyword threshold,
    # code-switched sentences, Tamil/Gujarati in Roman transliteration.
    ml_lang = _ml_detect_language(text)
    if ml_lang:
        logger.debug(f"ML detection result: {ml_lang} for text='{text[:40]}'")
        return ml_lang

    return "en"


def is_script_based(text: str) -> bool:
    """True if text contains non-Latin Indian script characters."""
    for pattern, _ in _SCRIPT_RANGES:
        if pattern.search(text):
            return True
    return False


class LanguageTracker:
    """
    Tracks language across a conversation for Deepgram STT switching only.

    Policy: Deepgram connection switches ONLY on an explicit verbal request
    ("speak in English", "hindi mein baat karo", "can we talk in Tamil?").
    Content-based detection is intentionally disabled — Indian callers
    routinely mix Hindi and English (Hinglish) regardless of their preferred
    language, and inferring a Deepgram swap from content causes mid-call
    disruption.

    LLM language mirroring is handled separately in the agent prompts
    via per-turn detected language, NOT by this tracker.
    """

    def __init__(self, initial_language: str = "hi", confirm_after: int = 2):
        self.current_language: str = initial_language
        self._confirm_after   = confirm_after
        self._candidate       = initial_language
        self._candidate_count = 0

    def update(self, text: str) -> tuple[str, bool]:
        """
        Feed a transcript. Returns (current_language, switched).
        switched=True means the Deepgram language just changed right now.

        Only switches when the caller explicitly names a language
        ("let's talk in Hindi", "please speak in English", "Tamil mein baat karo").
        All other input — including mixed Hinglish, single English words, and
        common greetings — is treated as the current language.
        """
        if _is_data_input(text):
            logger.debug(f"Data input detected, skipping lang detection: '{text}'")
            return self.current_language, False

        explicit = detect_explicit_language_request(text)
        if explicit and explicit != self.current_language:
            return self._switch(explicit)

        return self.current_language, False

    def _switch(self, lang: str) -> tuple[str, bool]:
        old = self.current_language
        self.current_language = lang
        self._candidate       = lang
        self._candidate_count = 0
        logger.info(f"Language switched: {old} → {lang}")

        # Sprint 4: Prometheus metrics
        try:
            from core.metrics import LANGUAGE_SWITCHES
            LANGUAGE_SWITCHES.labels(from_lang=old, to_lang=lang).inc()
        except Exception:
            pass

        return lang, True