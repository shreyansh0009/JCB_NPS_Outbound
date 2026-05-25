"""
CallSession: tracks state for a single phone call.

Persists:
  - which agent is currently active
  - full conversation history (for LLM context)
  - arbitrary metadata that agents can read/write (passed across handoffs)
  - detected language (for multilingual support)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from providers.language.detector import LanguageTracker


@dataclass
class CallSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    current_agent: str = ""          # name of active agent
    history: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    language_tracker: LanguageTracker = field(default_factory=LanguageTracker)

    @property
    def current_language(self) -> str:
        """The currently detected language code (e.g. 'hi', 'en', 'ta')."""
        return self.language_tracker.current_language

    def update_language(self, text: str) -> tuple[str, bool]:
        """
        Feed user transcript to the language tracker.
        Returns (language_code, switched) — switched=True when language changed.
        """
        return self.language_tracker.update(text)

    def set_language(self, lang_code: str) -> None:
        """Force-set the session language (e.g. from an external signal)."""
        self.language_tracker.current_language = lang_code
        self.language_tracker._candidate = lang_code
        self.language_tracker._candidate_count = 0

    def add_message(self, role: str, content: str) -> None:
        # Tag with current language so _chat can detect when language changed
        self.history.append({"role": role, "content": content, "lang": self.current_language})

    def switch_agent(self, agent_name: str, carry_history: bool = False) -> None:
        """
        Switch to a different agent.
        carry_history=True keeps the full conversation history (so the new
        agent has context). False resets history so the new agent starts fresh
        with only its own system prompt.
        """
        self.current_agent = agent_name
        if not carry_history:
            self.history = []

    def set(self, key: str, value: Any) -> None:
        self.metadata[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)

    # ── Serialization (for Redis session store) ───────────────────────────────

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for Redis storage."""
        return {
            "session_id":    self.session_id,
            "current_agent": self.current_agent,
            "history":       self.history,       # list[dict] — already JSON-safe
            "metadata":      self.metadata,       # dict[str, Any] — caller must ensure serializable
            "language":      self.current_language,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CallSession":
        """Restore a CallSession from a serialized dict."""
        session = cls(session_id=data["session_id"])
        session.current_agent = data.get("current_agent", "")
        session.history = data.get("history", [])
        session.metadata = data.get("metadata", {})
        lang = data.get("language", "hi")
        session.set_language(lang)
        return session
