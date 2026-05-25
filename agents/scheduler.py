"""
SchedulerAgent: books, reschedules, and cancels service/installation appointments.

MCP tools (from DMS):
  get_service_slots(date, service_type) -> available times
  book_service_appointment(name, phone, date, time, service_type) -> confirmation
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


class SchedulerAgent(BaseAgent):
    name = "scheduler"
    can_handoff_to = ["closer"]

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        reply = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply

        # Strip ALL control tags including [LANG:x]
        clean_reply = re.sub(
            r"\[HANDOFF:[^\]]+\]|\[END_CALL\]|\[LANG:[^\]]+\]",
            "", reply
        ).strip()

        if handoff:
            booked = session.get("mcp_book_service_appointment_result", {})
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
