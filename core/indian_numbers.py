from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "zeros": "0", "zeroes": "0",
    "one": "1", "won": "1",
    "ones": "1",
    "two": "2", "to": "2", "too": "2",
    "twos": "2",
    "three": "3", "tree": "3",
    "threes": "3",
    "four": "4", "for": "4",
    "fours": "4",
    "five": "5",
    "fives": "5",
    "six": "6", "sex": "6",
    "sixes": "6",
    "seven": "7",
    "sevens": "7",
    "eight": "8", "ate": "8",
    "eights": "8",
    "nine": "9",
    "nines": "9",
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4",
    "पांच": "5", "पाँच": "5", "छह": "6", "सात": "7", "आठ": "8", "नौ": "9",
    "ek": "1", "do": "2", "teen": "3", "char": "4", "chaar": "4", "paanch": "5",
    "chhe": "6", "cheh": "6", "saat": "7", "aath": "8", "nau": "9",
}

_TENS_WORDS: dict[str, str] = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    "seventy": "70", "eighty": "80", "ninety": "90",
}

_REPEAT_WORDS: dict[str, int] = {
    "double": 2, "triple": 3, "quadruple": 4,
    "doubles": 2, "triples": 3,
    "डबल": 2, "ट्रिपल": 3,
}

# "N times digit" / "N baar digit" patterns — maps the multiplier word/number
# Used for: "four times zero", "3 times nine", "do baar nau", "chaar baar zero"
_MULTIPLIER_WORDS: dict[str, int] = {
    # English digit words
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
    # Hindi digit words (romanized)
    "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
    "chhe": 6, "cheh": 6, "saat": 7, "aath": 8, "nau": 9,
    # Hindi digit words (Devanagari)
    "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5,
    "छह": 6, "सात": 7, "आठ": 8, "नौ": 9,
    # Numeric strings
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
}

# Words that mean "times" / "baar" — the connector between multiplier and digit
_TIMES_WORDS: set[str] = {
    "times", "time",
    "baar", "bar", "बार",
}

_HINDI_DIGIT_WORDS = {
    "0": "शून्य", "1": "एक", "2": "दो", "3": "तीन", "4": "चार",
    "5": "पाँच", "6": "छह", "7": "सात", "8": "आठ", "9": "नौ",
}
_ENGLISH_DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}
_HINDI_REPEAT_WORDS = {2: "डबल", 3: "ट्रिपल"}
_ENGLISH_REPEAT_WORDS = {2: "double", 3: "triple"}

_TOKEN_RE = re.compile(r"\+?\d+|[A-Za-z]+|[\u0900-\u097F]+|[^\w\s]", re.UNICODE)
_PIN_KEYWORD_RE = re.compile(r"\b(?:pin|pincode|pin code|postal code)\b|पिनकोड|पिन कोड|पिन", re.IGNORECASE)


@dataclass
class NumericParseResult:
    digits: str | None
    readback: str | None
    invalid_candidate: str | None = None


@dataclass
class _Chunk:
    digits: str
    repeated: bool = False
    repeat_count: int = 1


@dataclass
class _Span:
    digits: str
    chunks: list[_Chunk]


def _slice_chunks_from_right(chunks: list[_Chunk], keep_digits: int) -> list[_Chunk]:
    """
    Keep only the rightmost `keep_digits` digits while preserving original chunk style.
    This lets us strip +91 / leading 0 but still read back the caller's final number
    in the same double/triple pattern they spoke.
    """
    selected: list[_Chunk] = []
    remaining = keep_digits

    for chunk in reversed(chunks):
        if remaining <= 0:
            break
        if len(chunk.digits) <= remaining:
            selected.insert(0, chunk)
            remaining -= len(chunk.digits)
            continue

        sliced_digits = chunk.digits[-remaining:]
        selected.insert(
            0,
            _Chunk(
                digits=sliced_digits,
                repeated=chunk.repeated and len(set(sliced_digits)) == 1,
                repeat_count=len(sliced_digits),
            ),
        )
        remaining = 0

    return selected


def _digit_word(digit: str, lang: str) -> str:
    table = _HINDI_DIGIT_WORDS if lang == "hi" else _ENGLISH_DIGIT_WORDS
    return table.get(digit, digit)


def _repeat_word(count: int, lang: str) -> str:
    table = _HINDI_REPEAT_WORDS if lang == "hi" else _ENGLISH_REPEAT_WORDS
    return table.get(count, "")


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).translate(_DEVANAGARI_DIGITS)
    normalized = normalized.replace("-", " ").replace(".", " ").replace(",", " ")
    return normalized


def _single_digit_from_token(token: str) -> str | None:
    lower = token.lower()
    if len(lower) == 1 and lower.isdigit():
        return lower
    return _DIGIT_WORDS.get(lower)


def _two_digit_from_token(token: str, next_token: str | None = None) -> tuple[str | None, int]:
    lower = token.lower()
    if lower not in _TENS_WORDS:
        return None, 0
    value = _TENS_WORDS[lower]
    if next_token is None:
        return value, 1
    next_digit = _single_digit_from_token(next_token)
    if next_digit and value.endswith("0"):
        return value[0] + next_digit, 2
    return value, 1


def _scan_spans(text: str) -> list[_Span]:
    tokens = _TOKEN_RE.findall(_normalize(text))
    spans: list[_Span] = []
    current_chunks: list[_Chunk] = []
    current_digits: list[str] = []
    idx = 0

    def _flush() -> None:
        nonlocal current_chunks, current_digits
        if current_digits:
            spans.append(_Span(digits="".join(current_digits), chunks=list(current_chunks)))
        current_chunks = []
        current_digits = []

    while idx < len(tokens):
        token = tokens[idx]
        lower = token.lower()

        # Pattern 1: "double nine", "triple zero", "डबल नौ"
        if lower in _REPEAT_WORDS and idx + 1 < len(tokens):
            next_digit = _single_digit_from_token(tokens[idx + 1])
            if next_digit:
                count = _REPEAT_WORDS[lower]
                current_digits.append(next_digit * count)
                current_chunks.append(_Chunk(digits=next_digit * count, repeated=True, repeat_count=count))
                idx += 2
                continue

        # Pattern 2: "N times digit" / "N baar digit"
        # e.g. "four times zero" → 0000, "3 baar 5" → 555, "do baar nau" → 99
        if lower in _MULTIPLIER_WORDS and idx + 2 < len(tokens):
            mid = tokens[idx + 1].lower()
            if mid in _TIMES_WORDS:
                next_digit = _single_digit_from_token(tokens[idx + 2])
                if next_digit:
                    count = _MULTIPLIER_WORDS[lower]
                    current_digits.append(next_digit * count)
                    current_chunks.append(_Chunk(digits=next_digit * count, repeated=True, repeat_count=count))
                    idx += 3
                    continue

        if token.startswith("+") and token[1:].isdigit():
            digits = token[1:]
            current_digits.append(digits)
            current_chunks.append(_Chunk(digits=digits))
            idx += 1
            continue

        if token.isdigit():
            current_digits.append(token)
            current_chunks.append(_Chunk(digits=token))
            idx += 1
            continue

        single_digit = _single_digit_from_token(token)
        if single_digit:
            current_digits.append(single_digit)
            current_chunks.append(_Chunk(digits=single_digit))
            idx += 1
            continue

        two_digit, consumed = _two_digit_from_token(token, tokens[idx + 1] if idx + 1 < len(tokens) else None)
        if two_digit:
            current_digits.append(two_digit)
            current_chunks.append(_Chunk(digits=two_digit))
            idx += consumed
            continue

        _flush()
        idx += 1

    _flush()
    return spans


def _build_readback(chunks: list[_Chunk], lang: str) -> str:
    parts: list[str] = []
    for chunk in chunks:
        if chunk.repeated and len(set(chunk.digits)) == 1:
            if chunk.repeat_count in (2, 3):
                # "double nine", "triple zero"
                parts.append(f"{_repeat_word(chunk.repeat_count, lang)} {_digit_word(chunk.digits[0], lang)}")
            else:
                # "four times zero", "chaar baar zero"
                count_word = _digit_word(str(chunk.repeat_count), lang)
                times_word = "बार" if lang == "hi" else "times"
                parts.append(f"{count_word} {times_word} {_digit_word(chunk.digits[0], lang)}")
            continue
        for digit in chunk.digits:
            parts.append(_digit_word(digit, lang))
    return " ".join(part for part in parts if part).strip()


def _strip_mobile_prefix(candidate: str) -> str:
    digits = candidate
    if digits.startswith("0091") and len(digits) >= 14:
        digits = digits[4:]
    elif digits.startswith("091") and len(digits) >= 13:
        digits = digits[3:]
    elif digits.startswith("91") and len(digits) >= 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) >= 11:
        digits = digits[1:]
    return digits


def parse_indian_mobile(text: str, lang: str = "hi") -> NumericParseResult:
    spans = _scan_spans(text)
    invalid_candidate: str | None = None

    for span in spans:
        candidate = _strip_mobile_prefix(span.digits)
        if len(candidate) == 10 and re.fullmatch(r"[6-9]\d{9}", candidate):
            selected_chunks = (
                span.chunks
                if candidate == span.digits
                else _slice_chunks_from_right(span.chunks, keep_digits=10)
            )
            return NumericParseResult(digits=candidate, readback=_build_readback(selected_chunks, lang))
        if len(candidate) >= 7:
            invalid_candidate = candidate

    return NumericParseResult(digits=None, readback=None, invalid_candidate=invalid_candidate)


def count_parsed_digits(text: str) -> int:
    """Total count of recognized digit tokens across all spans in ``text``.

    Reuses the same tokenizer as ``parse_indian_mobile`` — handles Devanagari,
    romanized Hindi, English word forms, 'double N'/'triple N', and 'N times M'.
    Used by the streaming pipeline to decide whether an utterance is a partial
    phone number that should be held for continuation.
    """
    return sum(len(span.digits) for span in _scan_spans(text))


def parse_indian_pincode(text: str, lang: str = "hi") -> NumericParseResult:
    normalized = _normalize(text)
    match = _PIN_KEYWORD_RE.search(normalized)
    candidate_text = normalized[match.start():] if match else normalized
    spans = _scan_spans(candidate_text)
    invalid_candidate: str | None = None

    for span in spans:
        if len(span.digits) == 6:
            return NumericParseResult(digits=span.digits, readback=_build_readback(span.chunks, lang))
        if 3 <= len(span.digits) <= 8:
            invalid_candidate = span.digits

    return NumericParseResult(digits=None, readback=None, invalid_candidate=invalid_candidate)
