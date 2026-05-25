import pytest

from agents.hello import HelloAgent
from core.agent import HandoffSignal
from core.session import CallSession


class DummyLLM:
    def __init__(self, reply: str = ""):
        self.reply = reply
        self.last_messages = None

    async def chat(self, messages):
        self.last_messages = messages
        return self.reply

    async def stream_chat(self, messages):
        self.last_messages = messages
        if self.reply:
            yield self.reply

            


def _make_outbound_session() -> CallSession:
    session = CallSession(current_agent="hello")
    session.set("direction", "outbound")
    session.set("support_domain", "outbound")
    session.set("language", "hi")
    session.set("language_instruction", "")
    session.set("customer_name", "Rohit")
    session.set("name", "Rohit")
    session.set("product_name", "Montra Super")
    session.set("outbound_opening_stage", "identity")
    session.set("outbound_opening_unclear_count", 0)
    session.set("outbound_identity_confirmed", False)
    return session


@pytest.mark.asyncio
async def test_outbound_identity_confirmation_moves_to_availability_without_llm():
    llm = DummyLLM("unused")
    agent = HelloAgent(llm=llm, prompt="test")
    session = _make_outbound_session()

    response = await agent.handle("हाँ, मैं ही हूँ", session)

    assert response.handoff is None
    assert "मैं प्रिया बोल रही हूं" in response.text
    assert "2 से 3 मिनट" in response.text
    assert session.get("outbound_opening_stage") == "availability"
    assert session.get("outbound_identity_confirmed") is True
    assert llm.last_messages is None


@pytest.mark.asyncio
async def test_outbound_wrong_number_goes_to_closer_without_llm():
    llm = DummyLLM("unused")
    agent = HelloAgent(llm=llm, prompt="test")
    session = _make_outbound_session()

    response = await agent.handle("गलत नंबर", session)

    assert isinstance(response.handoff, HandoffSignal)
    assert response.handoff.target == "closer"
    assert "माफ़ कीजिएगा" in response.text
    assert session.get("outbound_opening_stage") == "done"
    assert llm.last_messages is None


@pytest.mark.asyncio
async def test_outbound_busy_after_identity_confirmation_goes_to_closer():
    llm = DummyLLM("unused")
    agent = HelloAgent(llm=llm, prompt="test")
    session = _make_outbound_session()
    session.set("outbound_opening_stage", "availability")
    session.set("outbound_identity_confirmed", True)

    response = await agent.handle("अभी time नहीं है", session)

    assert isinstance(response.handoff, HandoffSignal)
    assert response.handoff.target == "closer"
    assert "सुविधाजनक समय" in response.text
    assert session.get("outbound_opening_stage") == "done"
    assert llm.last_messages is None


@pytest.mark.asyncio
async def test_outbound_complaint_at_identity_hands_off_to_screener():
    llm = DummyLLM("unused")
    agent = HelloAgent(llm=llm, prompt="test")
    session = _make_outbound_session()

    response = await agent.handle("हाँ मैं ही हूँ, लेकिन गाड़ी start नहीं हो रही", session)

    assert isinstance(response.handoff, HandoffSignal)
    assert response.handoff.target == "screener"
    assert "मैं आपकी बात ज़रूर सुनूंगी" in response.text
    assert session.get("intent") == "हाँ मैं ही हूँ, लेकिन गाड़ी start नहीं हो रही"
    assert session.get("outbound_opening_stage") == "done"
    assert llm.last_messages is None
