"""
Sprint 6: Analytics — aggregates call outcome data for the ops dashboard.

All queries run against the SQLiteOutcomeStore (or any BaseOutcomeStore that
supports query_since). For very high-volume deployments (2L+ calls/day) these
queries can be moved to a read-replica or a BI tool like Metabase / Superset.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from core.call_outcome_store import BaseOutcomeStore

logger = logging.getLogger(__name__)


class Analytics:
    """
    Wraps a BaseOutcomeStore and provides aggregated metrics for the ops dashboard.

    All methods are async-safe and query the store on demand.
    For production at 50K+ calls/day, add an in-memory cache (TTL ~60s)
    to avoid hammering SQLite on every dashboard refresh.
    """

    def __init__(self, store: BaseOutcomeStore):
        self._store = store

    # ── Core aggregate: everything since N hours ago ──────────────────────────

    async def _records_last_hours(self, hours: int) -> list[dict]:
        since = time.time() - hours * 3600
        return await self._store.query_since(since)

    # ── Summary stats ─────────────────────────────────────────────────────────

    async def get_summary(self, hours: int = 24) -> dict[str, Any]:
        """
        Returns the top-level KPIs for the ops dashboard:
          total_calls, resolved, abandoned, error, max_duration
          resolution_rate %, avg_duration_s, avg_turns, handoff_rate %
        """
        records = await self._records_last_hours(hours)
        total = len(records)
        if total == 0:
            return _empty_summary(hours)

        by_outcome: Counter = Counter(r["outcome"] for r in records)
        resolved = by_outcome.get("resolved", 0)
        resolution_rate = round(resolved / total * 100, 1) if total else 0.0

        durations = [r["duration_s"] for r in records if r.get("duration_s", 0) > 0]
        avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0

        turns_all = [r["total_turns"] for r in records]
        avg_turns = round(sum(turns_all) / len(turns_all), 1) if turns_all else 0.0

        handoffs = [r["handoff_count"] for r in records]
        handoff_rate = round(
            sum(1 for h in handoffs if h > 0) / len(handoffs) * 100, 1
        ) if handoffs else 0.0

        return {
            "hours": hours,
            "total_calls": total,
            "resolved": resolved,
            "abandoned": by_outcome.get("abandoned", 0),
            "error": by_outcome.get("error", 0),
            "max_duration": by_outcome.get("max_duration", 0),
            "resolution_rate": resolution_rate,
            "avg_duration_s": avg_duration,
            "avg_turns": avg_turns,
            "handoff_rate": handoff_rate,
        }

    # ── Agent performance ─────────────────────────────────────────────────────

    async def get_agent_performance(self, hours: int = 24) -> list[dict[str, Any]]:
        """
        Per-agent breakdown:
          agent_name, calls_handled, resolution_rate, avg_turns, avg_duration_s

        An agent is counted for a call if it appears in agent_path.
        """
        records = await self._records_last_hours(hours)
        if not records:
            return []

        # Build per-agent stats
        agent_calls:    defaultdict[str, list[dict]] = defaultdict(list)
        for r in records:
            path: list[str] = r.get("agent_path") or []
            seen: set[str] = set()
            for ag in path:
                if ag and ag not in seen:
                    agent_calls[ag].append(r)
                    seen.add(ag)

        rows = []
        for agent, recs in sorted(agent_calls.items()):
            n = len(recs)
            resolved = sum(1 for r in recs if r["outcome"] == "resolved")
            avg_turns = round(
                sum(r["total_turns"] for r in recs) / n, 1
            ) if n else 0.0
            avg_dur = round(
                sum(r["duration_s"] for r in recs) / n, 1
            ) if n else 0.0
            rows.append({
                "agent": agent,
                "calls_handled": n,
                "resolution_rate": round(resolved / n * 100, 1) if n else 0.0,
                "avg_turns": avg_turns,
                "avg_duration_s": avg_dur,
            })

        # Sort by calls_handled descending
        rows.sort(key=lambda r: r["calls_handled"], reverse=True)
        return rows

    # ── Language distribution ─────────────────────────────────────────────────

    async def get_language_distribution(self, hours: int = 24) -> list[dict]:
        """Returns [{language, count, pct}] sorted by count desc."""
        records = await self._records_last_hours(hours)
        if not records:
            return []

        counter: Counter = Counter(
            r.get("language", "unknown") for r in records
        )
        total = len(records)
        return [
            {
                "language": lang,
                "count": count,
                "pct": round(count / total * 100, 1),
            }
            for lang, count in counter.most_common()
        ]

    # ── Hourly call volume ────────────────────────────────────────────────────

    async def get_hourly_volume(self, hours: int = 24) -> list[dict]:
        """
        Returns [{hour_label (HH:00), total, resolved, abandoned}] for the
        last `hours` hours, in chronological order.
        """
        records = await self._records_last_hours(hours)

        # Build hour buckets
        buckets: defaultdict[str, Counter] = defaultdict(Counter)
        for r in records:
            dt = datetime.fromtimestamp(r["started_at"], tz=timezone.utc)
            label = dt.strftime("%Y-%m-%dT%H:00")
            buckets[label]["total"] += 1
            buckets[label][r["outcome"]] += 1

        return [
            {
                "hour": label,
                "total": data["total"],
                "resolved": data.get("resolved", 0),
                "abandoned": data.get("abandoned", 0),
            }
            for label, data in sorted(buckets.items())
        ]

    # ── Recent calls log ──────────────────────────────────────────────────────

    async def get_recent_calls(self, limit: int = 50) -> list[dict]:
        """Returns the `limit` most recent call records for the call log table."""
        return await self._store.query_recent(limit=limit)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_summary(hours: int) -> dict:
    return {
        "hours": hours,
        "total_calls": 0,
        "resolved": 0,
        "abandoned": 0,
        "error": 0,
        "max_duration": 0,
        "resolution_rate": 0.0,
        "avg_duration_s": 0.0,
        "avg_turns": 0.0,
        "handoff_rate": 0.0,
    }
