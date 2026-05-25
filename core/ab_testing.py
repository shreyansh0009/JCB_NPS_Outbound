"""
Sprint 7: A/B testing for prompts and LLM settings.

Config: config/ab_tests.json
Assignment is deterministic by call_sid hash — same caller always same variant.
Results visible at /ops/ab-results.

ab_tests.json example:
{
  "tests": [
    {
      "test_id": "hello_greeting_v2",
      "description": "New empathetic Hindi greeting",
      "active": true,
      "traffic_split": 50,
      "variants": {
        "control":   {"prompt_file": "prompts/hello.md"},
        "treatment": {"prompt_file": "prompts/hello_v2.md"}
      }
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ABVariant:
    test_id: str
    variant_name: str        # "control" | "treatment"
    config: dict[str, Any]


@dataclass
class ABTest:
    test_id: str
    description: str
    active: bool
    traffic_split: int       # 0-100: % routed to "treatment"
    variants: dict[str, dict]


class ABTestingFramework:
    def __init__(self, config_path: str = "config/ab_tests.json"):
        self._path = Path(config_path)
        self._tests: dict[str, ABTest] = {}
        self._assignments: dict[str, dict[str, str]] = {}  # call_sid → {test_id: variant}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            data = json.load(f)
        for t in data.get("tests", []):
            self._tests[t["test_id"]] = ABTest(
                test_id=t["test_id"],
                description=t.get("description", ""),
                active=t.get("active", False),
                traffic_split=t.get("traffic_split", 50),
                variants=t.get("variants", {}),
            )
        logger.info(f"[ABTesting] Loaded {len(self._tests)} tests")

    def assign(self, call_sid: str) -> dict[str, ABVariant]:
        """Deterministic per-test assignment based on call_sid hash."""
        assignments: dict[str, ABVariant] = {}
        for test_id, test in self._tests.items():
            if not test.active:
                continue
            bucket = int(hashlib.sha256(f"{call_sid}:{test_id}".encode()).hexdigest()[:8], 16) % 100
            variant_name = "treatment" if bucket < test.traffic_split else "control"
            assignments[test_id] = ABVariant(
                test_id=test_id,
                variant_name=variant_name,
                config=test.variants.get(variant_name, {}),
            )
        self._assignments[call_sid] = {tid: v.variant_name for tid, v in assignments.items()}
        return assignments

    def apply_to_agent(self, agent, variant: ABVariant) -> None:
        """Apply variant config (prompt_file override) to agent."""
        pf = variant.config.get("prompt_file")
        if pf and Path(pf).exists():
            agent.system_prompt = Path(pf).read_text(encoding="utf-8")
            logger.info(f"[ABTesting] {variant.test_id}/{variant.variant_name} → {agent.name}")

    def get_summary(self) -> list[dict]:
        rows = []
        for test in self._tests.values():
            total = sum(1 for v in self._assignments.values() if test.test_id in v)
            treat = sum(1 for v in self._assignments.values() if v.get(test.test_id) == "treatment")
            rows.append({
                "test_id": test.test_id,
                "description": test.description,
                "active": test.active,
                "traffic_split": test.traffic_split,
                "total_calls": total,
                "treatment_calls": treat,
                "control_calls": total - treat,
            })
        return rows

    def reload(self) -> None:
        self._tests.clear()
        self._load()
