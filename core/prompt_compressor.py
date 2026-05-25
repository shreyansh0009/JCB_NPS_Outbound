"""
Sprint 3: Prompt compressor — trims RAG chunks to keep the system prompt short.

Why:
  Groq's llama-3.3-70b charges per input token and has lower throughput at
  longer prompts.  Service and sales agents use rag_top_k=5 (up to 5 chunks
  per query).  Compressing to the 2 most relevant chunks at ≤350 chars each
  saves ~40% of RAG-added tokens with negligible quality loss for voice calls
  (the agent speaks 15-30 words — it only needs enough context to answer the
  specific question, not an exhaustive knowledge dump).

Operations:
  1. Re-rank  — score each chunk by keyword overlap with the user query.
  2. Select   — keep top max_chunks (default 2).
  3. Trim     — cap each chunk at max_chars (default 350), break at word boundary.

No external dependencies — pure Python.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+")

_DEFAULT_MAX_CHUNKS = 2
_DEFAULT_MAX_CHARS  = 350   # chars per chunk — ≈ 90 tokens, fits comfortably in voice context


def _tokenize(text: str) -> set[str]:
    """Lower-cased word set, strips punctuation."""
    return {w.lower() for w in _WORD_RE.findall(text)}


def _overlap_score(query_tokens: set[str], chunk: str) -> int:
    """Count query words that appear in the chunk."""
    return len(query_tokens & _tokenize(chunk))


def compress_rag_chunks(
    query: str,
    chunks: list[str],
    max_chunks: int = _DEFAULT_MAX_CHUNKS,
    max_chars: int  = _DEFAULT_MAX_CHARS,
) -> list[tuple[int, str]]:
    """
    Select and trim the most relevant RAG chunks for a given user query.

    Args:
        query      : The user message used for retrieval (for re-ranking).
        chunks     : Raw chunk strings from the ChromaRetriever.
        max_chunks : Maximum number of chunks to keep (default 2).
        max_chars  : Maximum character length per chunk (default 350).

    Returns:
        List of (original_index, trimmed_text) tuples ordered by original index.
        Callers can use the original_index to look up metadata (source, score).
    """
    if not chunks:
        return []

    if len(chunks) <= max_chunks:
        # No re-ranking needed — just trim each chunk
        return [(i, _trim(c, max_chars)) for i, c in enumerate(chunks)]

    query_tokens = _tokenize(query)

    # Score each chunk; preserve original index for stable sort within same score
    scored = sorted(
        enumerate(chunks),
        key=lambda ic: (_overlap_score(query_tokens, ic[1]), -ic[0]),
        reverse=True,
    )

    top = scored[:max_chunks]
    # Restore original order within top-N so context reads naturally
    top.sort(key=lambda ic: ic[0])

    result = [(orig_idx, _trim(chunk, max_chars)) for orig_idx, chunk in top]

    logger.debug(
        f"[Compressor] {len(chunks)} chunks → {len(result)} selected "
        f"(indices={[i for i,_ in result]}), max_chars={max_chars}"
    )
    return result


def _trim(chunk: str, max_chars: int) -> str:
    """Trim a chunk to max_chars, breaking at the nearest word boundary."""
    if len(chunk) <= max_chars:
        return chunk
    trimmed = chunk[:max_chars]
    last_space = trimmed.rfind(" ")
    # Only break at word boundary if the space is in the last 20% of the window
    if last_space > int(max_chars * 0.80):
        trimmed = trimmed[:last_space]
    return trimmed + "…"
