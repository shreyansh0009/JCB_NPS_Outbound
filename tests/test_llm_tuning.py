import pytest

from config.settings import Settings
from core.agent import BaseAgent
from core.session import CallSession


class DummyLLM:
    async def chat(self, messages):
        return "ok"


class DummyAgent(BaseAgent):
    name = "dummy"

    async def handle(self, transcript: str, session):
        raise NotImplementedError


def test_settings_default_llm_tuning_is_voice_friendly():
    settings = Settings()

    assert settings.llm_temperature == pytest.approx(0.25)
    assert settings.llm_top_p == pytest.approx(0.85)
    assert settings.llm_frequency_penalty == pytest.approx(0.35)
    assert settings.llm_presence_penalty == pytest.approx(0.05)
    assert settings.llm_repeat_penalty == pytest.approx(1.08)
    assert settings.llm_max_tokens == 90


def test_build_messages_includes_emotion_adaptation_and_latency_rules():
    agent = DummyAgent(llm=DummyLLM(), prompt="base prompt")
    session = CallSession(current_agent="dummy")

    messages = agent._build_messages(
        session,
        user_message="I am very upset and worried about this repair",
        rag_context="",
    )

    system_message = messages[0]["content"]
    assert "Voice Response Style" in system_message
    assert "CURRENT CALLER EMOTION: sad or distressed." in system_message
    assert "Prefer 1 to 3 short sentences." in system_message
