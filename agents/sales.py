"""
SalesAgent (Godrej Appliances): product info, pricing, dealer location,
new purchase guidance.
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


class SalesAgent(BaseAgent):
    name = "sales"
    can_handoff_to = ["service", "closer"]

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        reply = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply
        clean_reply = re.sub(r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP):[^\]]*\]|\[END_CALL\]", "", reply).strip()

        if handoff:
            return AgentResponse(
                text=clean_reply,
                handoff=HandoffSignal(
                    target=handoff,
                    data={
                        "customer_name":   session.get("customer_name", ""),
                        "customer_mobile": session.get("customer_mobile", ""),
                        "language":        session.get("language", ""),
                    },
                ),
            )
        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)
        return AgentResponse(text=clean_reply)

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
