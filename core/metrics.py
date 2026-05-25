"""
Sprint 4: Prometheus metrics for the AI voice agent.

Exported at GET /metrics  — scraped by Prometheus every 15s.

Metric naming: voice_{component}_{measurement}_{unit}

Design decisions:
  - Histograms preferred over summaries (aggregatable across pods)
  - Labels kept minimal (high cardinality label = RAM explosion at 5K calls)
  - Buckets tuned for voice call latency profiles (ms-range for TTS/LLM)
  - Call outcome is the most important business metric — drives SLA reporting
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge, Info

# ── Call lifecycle ────────────────────────────────────────────────────────────

CALLS_TOTAL = Counter(
    "voice_calls_total",
    "Total calls handled, by outcome and primary language",
    ["outcome", "language"],
    # outcome: resolved | abandoned | max_duration | error | unknown
)

CALLS_ACTIVE = Gauge(
    "voice_calls_active",
    "Calls currently in progress across this worker",
)

CALLS_QUEUED = Gauge(
    "voice_calls_queued",
    "Calls currently waiting in the concurrency queue",
)

CALLS_REJECTED = Counter(
    "voice_calls_rejected_total",
    "Calls rejected because the queue was full",
)

CALL_DURATION = Histogram(
    "voice_call_duration_seconds",
    "Full call duration from connection to hangup",
    ["outcome"],
    buckets=[10, 30, 60, 120, 180, 300, 600],
)

# ── Turn latency (core SLA metric) ───────────────────────────────────────────
# Defined as: time from STT transcript ready → first TTS audio frame sent.
# Target: p95 ≤ 2.0s, p99 ≤ 3.5s

TURN_LATENCY = Histogram(
    "voice_turn_latency_seconds",
    "End-to-end turn latency: transcript ready → first audio frame out",
    ["agent"],
    buckets=[0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0],
)

TURNS_TOTAL = Counter(
    "voice_turns_total",
    "Total conversation turns processed",
    ["agent", "language"],
)

# ── LLM ──────────────────────────────────────────────────────────────────────

LLM_LATENCY = Histogram(
    "voice_llm_latency_seconds",
    "LLM response latency (TTFT — time to first token)",
    ["agent", "model_tier"],   # model_tier: fast (8b) | smart (70b)
    buckets=[0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 4.0],
)

LLM_ERRORS = Counter(
    "voice_llm_errors_total",
    "LLM call failures by type",
    ["agent", "error_type"],   # circuit_open | rate_limit | timeout | other
)

LLM_CACHE_HITS = Counter(
    "voice_llm_cache_hits_total",
    "LLM responses served from cache (skips Groq API call)",
    ["agent", "language"],
)

# ── STT ──────────────────────────────────────────────────────────────────────

STT_LATENCY = Histogram(
    "voice_stt_latency_seconds",
    "Deepgram endpointing latency (speech_final → transcript delivery)",
    ["provider"],
    buckets=[0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0],
)

STT_ERRORS = Counter(
    "voice_stt_errors_total",
    "STT connection/transcription errors",
    ["provider", "error_type"],
)

# ── TTS ──────────────────────────────────────────────────────────────────────

TTS_LATENCY = Histogram(
    "voice_tts_latency_seconds",
    "TTS first-audio-chunk latency",
    ["provider", "cache_hit"],  # cache_hit: true | false
    buckets=[0.02, 0.05, 0.075, 0.1, 0.15, 0.2, 0.4, 0.75, 1.5],
)

TTS_ERRORS = Counter(
    "voice_tts_errors_total",
    "TTS synthesis failures",
    ["provider", "error_type"],
)

TTS_FALLBACKS = Counter(
    "voice_tts_fallbacks_total",
    "TTS failovers from primary to secondary provider",
    ["from_provider", "to_provider"],
)

# ── RAG ──────────────────────────────────────────────────────────────────────

RAG_LATENCY = Histogram(
    "voice_rag_latency_seconds",
    "RAG retrieval latency (ChromaDB query + embedding)",
    ["agent", "cache_hit"],
    buckets=[0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0],
)

RAG_CACHE_HITS = Counter(
    "voice_rag_cache_hits_total",
    "RAG context served from cache (skips ChromaDB query)",
    ["agent"],
)

# ── Response cache (aggregate) ────────────────────────────────────────────────

CACHE_HITS = Counter(
    "voice_cache_hits_total",
    "Cache hits by tier",
    ["tier"],   # tts | llm | rag
)

CACHE_MISSES = Counter(
    "voice_cache_misses_total",
    "Cache misses by tier",
    ["tier"],
)

# ── Barge-in & language ───────────────────────────────────────────────────────

BARGE_INS_TOTAL = Counter(
    "voice_barge_ins_total",
    "Barge-in events: user interrupted agent while speaking",
)

LANGUAGE_SWITCHES = Counter(
    "voice_language_switches_total",
    "In-call language switches",
    ["from_lang", "to_lang"],
)

LANGUAGE_CALLS = Counter(
    "voice_calls_by_language_total",
    "Calls by detected primary language",
    ["language"],
)

# ── Circuit breakers ──────────────────────────────────────────────────────────

CIRCUIT_BREAKER_STATE = Gauge(
    "voice_circuit_breaker_state",
    "Circuit breaker state per provider (0=closed, 1=open, 2=half_open)",
    ["provider"],
)

CIRCUIT_BREAKER_TRIPS = Counter(
    "voice_circuit_breaker_trips_total",
    "Circuit breaker trips CLOSED→OPEN per provider",
    ["provider"],
)

CIRCUIT_BREAKER_RECOVERIES = Counter(
    "voice_circuit_breaker_recoveries_total",
    "Circuit breaker recoveries OPEN/HALF_OPEN→CLOSED per provider",
    ["provider"],
)

# ── System ────────────────────────────────────────────────────────────────────

ERRORS_TOTAL = Counter(
    "voice_errors_total",
    "Unhandled errors by component",
    ["component", "error_type"],
)

BUILD_INFO = Info(
    "voice_build",
    "Build and version information",
)

# ── Convenience: numeric state map for CIRCUIT_BREAKER_STATE gauge ────────────

CB_STATE_VALUES = {
    "closed":    0,
    "open":      1,
    "half_open": 2,
}
