"""
Interactive text chat — test the full agent pipeline without a phone call.

Usage:
    python chat.py                          # uses settings from .env
    python chat.py --agent service          # start on a specific agent
    python chat.py --no-rag --no-mcp        # disable optional capabilities
    python chat.py --mock-llm               # stub LLM (no API key needed)

What this tests end-to-end:
  - Agent routing and handoffs (hello → service / sales → closer)
  - RAG retrieval (ChromaDB + embeddings)
  - MCP tool calls (mocked or real HTTP)
  - Session history and metadata
  - All agent prompts and logic

What it does NOT test (phone-call specific):
  - Deepgram STT / Cartesia TTS / Twilio WebSocket
  - Sentence-level streaming and audio chunking
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.WARNING,  # suppress info noise; set to DEBUG for verbose
    format="%(levelname)s %(name)s — %(message)s",
)

# ── ANSI colour helpers ────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
YELLOW= "\033[33m"
RED   = "\033[31m"
BLUE  = "\033[34m"


def _col(code: str, text: str) -> str:
    return f"{code}{text}{RESET}"


# ── Mock LLM (no API key required) ────────────────────────────────────────────

class _MockLLM:
    """Stub LLM that echoes a scripted reply so you can test routing without keys."""

    async def chat(self, messages: list[dict]) -> str:
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        ).lower()

        # Simulate routing from hello agent
        if "service" in user_msg or "oil" in user_msg or "repair" in user_msg or "appointment" in user_msg:
            return "[HANDOFF:service]"
        if "buy" in user_msg or "sales" in user_msg or "car" in user_msg or "test drive" in user_msg or "rav4" in user_msg or "camry" in user_msg:
            return "[HANDOFF:sales]"
        if "bye" in user_msg or "thank" in user_msg or "done" in user_msg:
            return "[END_CALL]"

        agent_name = next(
            (m["content"].split("\n")[0].replace("#", "").strip()
             for m in messages if m["role"] == "system"),
            "Agent",
        )
        return f"[Mock {agent_name}] I received: \"{messages[-1]['content']}\". (This is a mock LLM response — set a real API key to test fully.)"

    async def stream_chat(self, messages):
        reply = await self.chat(messages)
        yield reply


# ── Session display helpers ────────────────────────────────────────────────────

def _print_separator():
    print(_col(DIM, "─" * 60))


def _print_response(agent_name: str, text: str, handoff: str | None, end_call: bool):
    tag = _col(CYAN + BOLD, f"[{agent_name.upper()}]")
    print(f"\n{tag} {text}")
    if handoff:
        print(_col(YELLOW, f"  ↪ Handoff → {handoff}"))
    if end_call:
        print(_col(RED, "  ✗ Call ended"))


def _print_session_state(session):
    meta = {k: v for k, v in session.metadata.items() if not k.startswith("mcp_")}
    mcp  = {k[4:]: v for k, v in session.metadata.items() if k.startswith("mcp_")}
    parts = [_col(DIM, f"  session={session.session_id[:8]}")]
    parts.append(_col(DIM, f"  turns={len([m for m in session.history if m['role']=='user'])}"))
    if meta:
        parts.append(_col(DIM, f"  meta={meta}"))
    if mcp:
        parts.append(_col(GREEN + DIM, f"  mcp={list(mcp.keys())}"))
    print("".join(parts))


# ── Main REPL ──────────────────────────────────────────────────────────────────

async def run_chat(args: argparse.Namespace):
    # ── Build LLM ─────────────────────────────────────────────────────────────
    if args.mock_llm:
        llm = _MockLLM()
        print(_col(YELLOW, "Using mock LLM (no API key required)"))
    else:
        from config.settings import Settings
        settings = Settings.from_env()
        provider = settings.llm_provider

        if provider == "groq":
            from providers.llm.groq_provider import GroqLLM
            llm = GroqLLM(
                api_key=settings.groq_api_key,
                model=settings.groq_model,
                temperature=settings.llm_temperature,
            )
            print(_col(GREEN, f"LLM: Groq ({settings.groq_model})"))
        elif provider == "litellm":
            from providers.llm.litellm_provider import LiteLLMProvider
            llm = LiteLLMProvider.from_env()
            print(_col(GREEN, "LLM: LiteLLM"))
        else:
            from providers.llm.ollama import OllamaLLM
            llm = OllamaLLM(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                temperature=settings.llm_temperature,
            )
            print(_col(GREEN, f"LLM: Ollama ({settings.ollama_model})"))

    # ── Build orchestrator ─────────────────────────────────────────────────────
    from config.settings import Settings
    settings = Settings.from_env()

    squad_path = settings.squad_path
    if args.no_rag or args.no_mcp:
        # Patch env to disable selectively
        if args.no_rag:
            os.environ["EMBEDDING_PROVIDER"] = "__disabled__"
        if args.no_mcp:
            # Point to a non-existent config so MCPRegistry is empty
            os.environ["MCP_SERVERS_PATH"] = "__disabled__"

    from agents.registry import build_orchestrator
    orchestrator = build_orchestrator(squad_path, llm)

    # ── Start session ──────────────────────────────────────────────────────────
    session = orchestrator.start_session()

    # Optionally start on a specific agent
    if args.agent and args.agent != session.current_agent:
        if args.agent in orchestrator._agents:
            session.switch_agent(args.agent)
        else:
            print(_col(RED, f"Unknown agent '{args.agent}'. Available: {list(orchestrator._agents.keys())}"))
            return

    # ── Print welcome ──────────────────────────────────────────────────────────
    print()
    print(_col(BOLD, "Sunrise Auto Group — Voice Agent Chat Test"))
    print(_col(DIM, "Type your message and press Enter. Commands: /agent, /session, /reset, /quit"))
    _print_separator()

    agents_list = ", ".join(orchestrator._agents.keys())
    print(_col(DIM, f"Agents loaded: {agents_list}"))
    print(_col(DIM, f"Starting agent: {session.current_agent}"))
    _print_separator()

    # ── REPL ──────────────────────────────────────────────────────────────────
    while True:
        try:
            prompt_label = _col(CYAN + BOLD, f"you [{session.current_agent}]")
            line = input(f"\n{prompt_label}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(_col(DIM, "\nBye."))
            break

        if not line:
            continue

        # ── Slash commands ─────────────────────────────────────────────────
        if line.startswith("/"):
            cmd = line[1:].split()[0].lower()
            rest = line[len(cmd) + 2:].strip()

            if cmd in ("quit", "exit", "q"):
                print(_col(DIM, "Bye."))
                break

            elif cmd == "agent":
                if rest:
                    if rest in orchestrator._agents:
                        session.switch_agent(rest)
                        print(_col(YELLOW, f"Switched to agent: {rest}"))
                    else:
                        print(_col(RED, f"Unknown: {rest}. Available: {list(orchestrator._agents.keys())}"))
                else:
                    print(f"Current: {session.current_agent} | Available: {list(orchestrator._agents.keys())}")

            elif cmd == "session":
                print(f"  session_id : {session.session_id}")
                print(f"  current    : {session.current_agent}")
                print(f"  history    : {len(session.history)} messages")
                print(f"  metadata   : {session.metadata}")

            elif cmd == "reset":
                session = orchestrator.start_session()
                if args.agent:
                    session.switch_agent(args.agent)
                print(_col(YELLOW, f"Session reset. Starting with: {session.current_agent}"))

            elif cmd == "history":
                for m in session.history:
                    role_col = CYAN if m["role"] == "user" else GREEN
                    print(_col(role_col, f"  [{m['role']}] {m['content'][:120]}"))

            elif cmd == "debug":
                logging.getLogger().setLevel(logging.DEBUG)
                print(_col(YELLOW, "Debug logging enabled"))

            else:
                print(_col(DIM, "Commands: /agent [name], /session, /history, /reset, /debug, /quit"))
            continue

        # ── Send to orchestrator ───────────────────────────────────────────
        try:
            response = await orchestrator.process(line, session)
        except Exception as e:
            print(_col(RED, f"Error: {e}"))
            if logging.getLogger().level <= logging.DEBUG:
                import traceback; traceback.print_exc()
            continue

        handoff_target = response.handoff.target if response.handoff else None
        _print_response(
            agent_name=session.current_agent,
            text=response.text,
            handoff=handoff_target,
            end_call=response.end_call,
        )
        _print_session_state(session)

        if response.end_call:
            print(_col(RED + BOLD, "\nCall ended by agent."))
            break


def main():
    parser = argparse.ArgumentParser(description="Chat with the automotive voice agents (no phone call)")
    parser.add_argument("--agent", default="", help="Start on a specific agent (hello/service/sales/closer)")
    parser.add_argument("--mock-llm", action="store_true", help="Use stub LLM (no API key needed)")
    parser.add_argument("--no-rag", action="store_true", help="Disable RAG retrieval")
    parser.add_argument("--no-mcp", action="store_true", help="Disable MCP tool calls")
    args = parser.parse_args()
    asyncio.run(run_chat(args))


if __name__ == "__main__":
    main()
