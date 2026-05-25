"""
Orchestrator: owns the agent registry and drives the call loop.

Responsibilities:
  1. Load agents from the squad definition (config/squads/*.json)
  2. Route each user turn to the currently-active agent
  3. Execute handoffs when an agent signals one
  4. End the call when an agent signals end_call=True
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, squad_path: str | Path):
        self._agents: dict[str, BaseAgent] = {}
        self._leader: str = ""
        self._squad_path = Path(squad_path)

    def register(self, agent: BaseAgent) -> None:
        """Register an agent instance. Call this for every agent before starting calls."""
        self._agents[agent.name] = agent
        logger.info(f"Registered agent: {agent.name}")

    def load_squad(self) -> None:
        """Load squad definition JSON and set the leader agent."""
        with open(self._squad_path) as f:
            squad = json.load(f)
        self._leader = squad["leader"]
        logger.info(f"Squad loaded: {squad['name']}, leader={self._leader}")

    def start_session(self) -> CallSession:
        """Create a new session and point it at the leader agent."""
        if not self._leader:
            raise RuntimeError("Squad not loaded. Call load_squad() first.")
        session = CallSession(current_agent=self._leader)
        logger.info(f"New session {session.session_id} -> agent={self._leader}")
        return session
    _MAX_HANDOFF_CHAIN = 5

    async def process(self, transcript: str, session: CallSession) -> AgentResponse:
        """
        Main entry point per user turn.
        Handles handoff chaining: if an agent hands off, the target agent
        immediately handles the same transcript so the caller hears a
        seamless response. Chains through multiple handoffs (e.g.
        hello → screener → service) until an agent responds without handing off.
        """
        agent = self._agents.get(session.current_agent)
        if not agent:
            raise ValueError(f"No agent registered for name: '{session.current_agent}'")
 
        response = await agent.handle(transcript, session)
 
        for _ in range(self._MAX_HANDOFF_CHAIN):
            if not response.handoff:
                break
 
            target_name = response.handoff.target
            if target_name not in self._agents:
                logger.error(f"Handoff target '{target_name}' not found in registry")
                break
 
            source_name = session.current_agent
            session.metadata.update(response.handoff.data)
            session.switch_agent(target_name, carry_history=True)
            logger.info(f"Handoff: {source_name} -> {target_name} (session={session.session_id})")
 
            target_agent = self._agents[target_name]
            response = await target_agent.handle(transcript, session)
 
        return response
 
     

    async def stream_process(self, transcript: str, session):
        """
        Streaming version of process().
 
        Yields (sentence, None) for each TTS-ready sentence as the LLM streams,
        then yields (None, AgentResponse) when done.
 
        On handoff: chains through multiple agents (e.g. hello → screener →
        service) until one responds without handing off. Each agent in the
        chain is streamed so partial sentences reach TTS immediately.
        """
        agent = self._agents.get(session.current_agent)
        if not agent:
            raise ValueError(f"No agent registered for name: '{session.current_agent}'")
 
        final_response = None
        async for sentence, resp in agent.stream_handle(transcript, session):
            if sentence:
                yield sentence, None
            if resp is not None:
                final_response = resp
 
        if final_response is None:
            yield None, AgentResponse(text="")
            return
 
        # Chain through handoffs until an agent responds without handing off
        for _ in range(self._MAX_HANDOFF_CHAIN):
            if not final_response.handoff:
                break
 
            target_name = final_response.handoff.target
            if target_name not in self._agents:
                logger.error(f"Handoff target '{target_name}' not found in registry")
                break
 
            source_name = session.current_agent
            session.metadata.update(final_response.handoff.data)
            session.switch_agent(target_name, carry_history=True)
            logger.info(f"Handoff: {source_name} -> {target_name} (session={session.session_id})")
 
            target_agent = self._agents[target_name]
            final_response = None
            async for sentence, resp in target_agent.stream_handle(transcript, session):
                if sentence:
                    yield sentence, None
                if resp is not None:
                    final_response = resp
 
            if final_response is None:
                final_response = AgentResponse(text="")
 
        yield None, final_response
 
 
       


    async def _execute_handoff(
        self,
        signal: HandoffSignal,
        session: CallSession,
        current_response: AgentResponse,
    ) -> AgentResponse:
        if signal.target not in self._agents:
            logger.error(f"Handoff target '{signal.target}' not found in registry")
            return current_response

        # Validate the handoff is allowed
        source_agent = self._agents[session.current_agent]
        if signal.target not in source_agent.can_handoff_to:
            logger.warning(
                f"Agent '{source_agent.name}' tried to hand off to "
                f"'{signal.target}' which is not in can_handoff_to"
            )

        # Merge any data the source agent wants to pass along
        session.metadata.update(signal.data)

        # Switch to the target agent (carry history by default so context flows)
        session.switch_agent(signal.target, carry_history=True)

        logger.info(
            f"Handoff: {source_agent.name} -> {signal.target} "
            f"(session={session.session_id})"
        )

        # Return current response with the transition message (if any) set.
        # The transition message is spoken *before* the new agent takes over.
        # The caller (pipeline) is responsible for speaking it.
        return AgentResponse(
            text=signal.message or current_response.text,
            handoff=signal,
        )
