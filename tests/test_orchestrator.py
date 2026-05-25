"""
Unit tests for the Orchestrator and agent handoff logic.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.agent import AgentResponse, HandoffSignal
from core.session import CallSession
from core.orchestrator import Orchestrator


class MockAgent:
    def __init__(self, name, can_handoff_to=None, response=None):
        self.name = name
        self.can_handoff_to = can_handoff_to or []
        self._response = response or AgentResponse(text="Hello")

    async def handle(self, transcript, session):
        return self._response


def make_orchestrator(agents):
    """Helper: build an orchestrator with a mock squad."""
    import json, tempfile, os
    squad = {
        "name": "Test Squad",
        "leader": agents[0].name,
        "agents": [{"name": a.name} for a in agents],
    }
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(squad, tmp)
    tmp.close()

    orch = Orchestrator(tmp.name)
    orch.load_squad()
    for agent in agents:
        orch.register(agent)

    os.unlink(tmp.name)
    return orch


@pytest.mark.asyncio
async def test_basic_response():
    agent = MockAgent("hello", response=AgentResponse(text="Hi there!"))
    orch = make_orchestrator([agent])
    session = orch.start_session()

    response = await orch.process("Hello", session)
    assert response.text == "Hi there!"
    assert session.current_agent == "hello"


@pytest.mark.asyncio
async def test_handoff_switches_agent():
    hello = MockAgent(
        "hello",
        can_handoff_to=["service"],
        response=AgentResponse(
            text="Connecting you now",
            handoff=HandoffSignal(target="service"),
        ),
    )
    service = MockAgent("service")

    orch = make_orchestrator([hello, service])
    session = orch.start_session()

    assert session.current_agent == "hello"
    await orch.process("Hi", session)
    assert session.current_agent == "service"


@pytest.mark.asyncio
async def test_handoff_passes_data():
    hello = MockAgent(
        "hello",
        can_handoff_to=["service"],
        response=AgentResponse(
            text="",
            handoff=HandoffSignal(target="service", data={"caller_name": "Jane"}),
        ),
    )
    service = MockAgent("service")

    orch = make_orchestrator([hello, service])
    session = orch.start_session()
    await orch.process("Hi", session)

    assert session.get("caller_name") == "Jane"


@pytest.mark.asyncio
async def test_unknown_handoff_target_does_not_crash():
    hello = MockAgent(
        "hello",
        can_handoff_to=["service"],
        response=AgentResponse(
            text="Going somewhere",
            handoff=HandoffSignal(target="nonexistent_agent"),
        ),
    )
    orch = make_orchestrator([hello])
    session = orch.start_session()

    # Should not raise — just log an error and return current response
    response = await orch.process("Hi", session)
    assert response is not None
