"""
Sprint 7: Prompt hot-reload — updates agent system prompts without restart.

Two mechanisms:
  1. Background polling every POLL_INTERVAL_S seconds (passive, zero overhead)
  2. POST /admin/reload-prompts — manual trigger, immediate effect

Usage in registry.py:
    loader = PromptLoader()
    loader.register("hello", hello_agent)
    asyncio.create_task(loader.start_polling())
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 30


def prompt_sha(text: str) -> str:
    """12-char prompt fingerprint for startup logs + cache invalidation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class _HasPrompt(Protocol):
    name: str
    system_prompt: str


class PromptLoader:
    def __init__(self, prompts_dir: str = "prompts"):
        self._dir = Path(prompts_dir)
        self._agents: dict[str, list[_HasPrompt]] = {}
        self._mtimes: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.reload_count = 0

    def register(self, prompt_name: str, agent: _HasPrompt) -> None:
        self._agents.setdefault(prompt_name, []).append(agent)
        path = self._dir / f"{prompt_name}.md"
        if path.exists():
            self._mtimes[prompt_name] = path.stat().st_mtime
        logger.info(f"[PromptLoader] Registered '{agent.name}' for prompt '{prompt_name}'")

    async def reload_all(self, force: bool = False) -> dict[str, bool]:
        results: dict[str, bool] = {}
        async with self._lock:
            for name, agents in self._agents.items():
                path = self._dir / f"{name}.md"
                if not path.exists():
                    results[name] = False
                    continue
                mtime = path.stat().st_mtime
                if force or mtime > self._mtimes.get(name, 0):
                    content = path.read_text(encoding="utf-8")
                    for agent in agents:
                        agent.system_prompt = content
                    self._mtimes[name] = mtime
                    self.reload_count += 1
                    results[name] = True
                    logger.info(f"[PromptLoader] Reloaded '{name}' → {[a.name for a in agents]}")
                else:
                    results[name] = False
        return results

    async def start_polling(self) -> None:
        logger.info(f"[PromptLoader] Poll started (interval={POLL_INTERVAL_S}s)")
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            changed = await self.reload_all()
            if any(changed.values()):
                logger.info(f"[PromptLoader] Auto-reloaded: {[k for k,v in changed.items() if v]}")

    def fingerprints(self) -> dict[str, str]:
        """Map of prompt_name → 12-char SHA. Logged at startup to verify
        which prompt revision is live (independent of git/file caches)."""
        out: dict[str, str] = {}
        for name, agents in self._agents.items():
            if agents:
                out[name] = prompt_sha(agents[0].system_prompt or "")
        return out


# ── module-level singleton ────────────────────────────────────────────────────

_loader: Optional[PromptLoader] = None


def set_loader(loader: PromptLoader) -> None:
    """Install the active PromptLoader. Called once from build_orchestrator()."""
    global _loader
    _loader = loader


def get_loader() -> Optional[PromptLoader]:
    """Return the active PromptLoader, or None if not yet wired."""
    return _loader
