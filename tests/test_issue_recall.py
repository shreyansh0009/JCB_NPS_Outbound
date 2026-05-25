import pytest

from agents.hello import HelloAgent
from agents.screener import ScreenerAgent
from agents.service import ServiceAgent
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


@pytest.mark.asyncio
async def test_hello_captures_issue_before_name_and_mobile():
    agent = HelloAgent(llm=DummyLLM("[NAME:वर्षा]"), prompt="test")
    session = CallSession(current_agent="hello")
    session.set("language", "hi")

    await agent.handle("मेरा washing machine नहीं चल रहा है", session)

    assert session.get("intent") == "मेरा washing machine नहीं चल रहा है"
    assert session.get("reported_issue") == "मेरा washing machine नहीं चल रहा है"


@pytest.mark.asyncio
async def test_screener_routes_directly_on_confirmation_when_issue_known():
    agent = ScreenerAgent(llm=DummyLLM("unused"), prompt="test")
    session = CallSession(current_agent="screener")
    session.set("language", "hi")
    session.set("intent", "मेरा washing machine नहीं चल रहा है")

    response = await agent.handle("हां", session)

    assert isinstance(response.handoff, HandoffSignal)
    assert response.handoff.target == "service"


@pytest.mark.asyncio
async def test_service_prompt_includes_known_issue_context():
    llm = DummyLLM("ठीक है, क्या मशीन का power button press करने पर कोई light आती है?")
    agent = ServiceAgent(llm=llm, prompt="test")
    session = CallSession(current_agent="service")
    session.set("language", "hi")
    session.set("intent", "मेरा washing machine नहीं चल रहा है")
    session.set("name", "वर्षा")
    session.set("mobile", "8795583362")

    await agent.handle("हां", session)

    combined_messages = "\n".join(message["content"] for message in llm.last_messages)
    assert "CRITICAL KNOWN ISSUE: मेरा washing machine नहीं चल रहा है" in combined_messages
    assert "Do NOT ask the issue again" in combined_messages


@pytest.mark.asyncio
async def test_service_strict_refusal_for_pest_issue_without_llm_call():
    llm = DummyLLM("यह reply use नहीं होना चाहिए")
    agent = ServiceAgent(llm=llm, prompt="test")
    session = CallSession(current_agent="service")
    session.set("language", "hi")
    session.set("intent", "मेरे fridge में चूहा घुस गया है")

    response = await agent.handle("मेरे fridge में चूहा घुस गया है", session)

    assert response.end_call is True
    assert "मैं सिर्फ Godrej Appliances की technical service booking के लिए responsible हूँ" in response.text
    assert llm.last_messages is None


@pytest.mark.asyncio
async def test_service_strict_refusal_for_theft_issue_without_llm_call():
    llm = DummyLLM("यह reply use नहीं होना चाहिए")
    agent = ServiceAgent(llm=llm, prompt="test")
    session = CallSession(current_agent="service")
    session.set("language", "hi")
    session.set("intent", "मेरा refrigerator चोरी हो गया")

    response = await agent.handle("मेरा refrigerator चोरी हो गया", session)

    assert response.end_call is True
    assert "चोरी के मामले में कृपया पुलिस की मदद लें" in response.text
    assert llm.last_messages is None
