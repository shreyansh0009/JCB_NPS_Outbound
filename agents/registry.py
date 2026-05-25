"""
AgentRegistry: loads all agents and wires them into the Orchestrator.

Handles:
  - Loading prompts from /prompts/*.md
  - Setting up RAG retrievers per agent (using ChromaDB + embeddings)
  - Setting up MCP registries per agent (pointing at external tool servers)
  - Registering all agents with the orchestrator

To add a new agent:
  1. Create agents/my_agent.py
  2. Add prompt to prompts/my_agent.md
  3. Register it below
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.orchestrator import Orchestrator
from core.prompt_loader import PromptLoader, set_loader
from agents.hello import HelloAgent
from agents.screener import ScreenerAgent
from agents.service import ServiceAgent
from agents.sales import SalesAgent
from agents.scheduler import SchedulerAgent
from agents.closer import CloserAgent

logger = logging.getLogger(__name__)


def _load_prompt(name: str, prompts_dir: str = "prompts") -> str:
    path = Path(__file__).parent.parent / prompts_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _build_rag(collection_name: str, embeddings):
    """
    Build a ChromaRetriever for a given collection.
    Returns None if chromadb is not installed or collection is empty.
    """
    try:
        from providers.rag.chroma import ChromaRetriever
        return ChromaRetriever(
            collection_name=collection_name,
            embeddings=embeddings,
            persist_dir=os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
        )
    except ImportError:
        logger.warning("chromadb not installed — RAG disabled. pip install chromadb")
        return None
    except Exception as e:
        logger.warning(f"Could not init RAG collection '{collection_name}': {e}")
        return None


def _build_mcp_registry(server_configs: list[dict], tool_map: dict[str, str]):
    """
    Build an MCPRegistry from a list of server configs and explicit tool→server mappings.
    """
    try:
        from providers.mcp.registry import MCPRegistry
        registry = MCPRegistry()
        for cfg in server_configs:
            registry.add_server_from_config(cfg)
        for tool_name, server_name in tool_map.items():
            try:
                registry.register_tool(tool_name, server_name)
            except ValueError as e:
                logger.warning(f"MCP tool registration failed: {e}")
        return registry
    except Exception as e:
        logger.warning(f"Could not init MCP registry: {e}")
        return None


def _load_mcp_config() -> list[dict]:
    """Load MCP server configs from config/mcp_servers.json if present."""
    path = Path(__file__).parent.parent / "config" / "mcp_servers.json"
    if path.exists():
        with open(path) as f:
            return json.load(f).get("servers", [])
    return []


def build_orchestrator(squad_path: str, llm, prompts_dir: str = "prompts") -> Orchestrator:
    """
    Build and return a fully-wired Orchestrator.
    RAG and MCP are enabled if dependencies are installed and configured.

    Args:
        squad_path:   Path to squad JSON defining agent graph and RAG mappings.
        llm:          LLM provider instance shared by all agents.
        prompts_dir:  Directory under project root holding {agent_name}.md prompts.
                      Default "prompts" is the inbound set; pass "outbound_prompts"
                      for the outbound orchestrator.
    """
    orchestrator = Orchestrator(squad_path)
    orchestrator.load_squad()

    # ── Load squad JSON to get RAG collection names per agent ──────────────
    with open(squad_path) as f:
        squad_def = json.load(f)

    # Build a map of agent_name -> rag_collection from squad definition
    rag_map: dict[str, str] = {}
    for agent_def in squad_def.get("agents", []):
        if "rag_collection" in agent_def:
            rag_map[agent_def["name"]] = agent_def["rag_collection"]

    # ── Embeddings (shared across all RAG retrievers) ──────────────────────
    embeddings = None
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "ollama")
    try:
        from providers.rag.embeddings import get_embeddings
        embeddings = get_embeddings()
        logger.info(f"Embeddings provider: {embedding_provider}")
    except Exception as e:
        logger.warning(f"Embeddings not available ({e}) — RAG disabled")

    # ── RAG: build retrievers from squad-defined collection names ──────────
    service_rag = _build_rag(rag_map["service"], embeddings) if embeddings and "service" in rag_map else None
    sales_rag = _build_rag(rag_map["sales"], embeddings) if embeddings and "sales" in rag_map else None

    # ── MCP: both service and sales share the DMS server ──────────────────
    mcp_servers = _load_mcp_config()
    dms_mcp = None
    if mcp_servers:
        dms_tools = {
            "get_service_slots": "dms",
            "book_service_appointment": "dms",
            "get_inventory": "dms",
            "book_test_drive": "dms",
        }
        dms_mcp = _build_mcp_registry(mcp_servers, dms_tools)
        if dms_mcp:
            logger.info(f"DMS MCP tools registered: {dms_mcp.list_tools()}")

    # ── Persona (optional — loaded once and prepended to every agent prompt)
    # Reinforces identity, language rules, and conduct across handoffs.
    persona_path = Path(__file__).parent.parent / prompts_dir / "persona.md"
    persona_text = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""
    if persona_text:
        logger.info(f"Loaded persona from {prompts_dir}/persona.md ({len(persona_text)} chars)")

    # ── Wire all agents from squad definition ──────────────────────────────
    agents = [
        HelloAgent(
            llm=llm,
            prompt=_load_prompt("hello", prompts_dir),
            persona=persona_text,
        ),
        ScreenerAgent(
            llm=llm,
            prompt=_load_prompt("screener", prompts_dir),
            persona=persona_text,
        ),
        ServiceAgent(
            llm=llm,
            prompt=_load_prompt("service", prompts_dir),
            persona=persona_text,
            rag=service_rag,
            mcp_registry=dms_mcp,
            rag_top_k=3,
        ),
        SalesAgent(
            llm=llm,
            prompt=_load_prompt("sales", prompts_dir),
            persona=persona_text,
            rag=sales_rag,
            mcp_registry=dms_mcp,
            rag_top_k=4,
        ),
        SchedulerAgent(
            llm=llm,
            prompt=_load_prompt("scheduler", prompts_dir),
            persona=persona_text,
            mcp_registry=dms_mcp,
        ),
        CloserAgent(
            llm=llm,
            prompt=_load_prompt("closer", prompts_dir),
            persona=persona_text,
        ),
    ]

    for agent in agents:
        rag_label = "RAG" if getattr(agent, "rag", None) else ""
        mcp_label = "MCP" if getattr(agent, "mcp_registry", None) else ""
        capabilities = ", ".join(filter(None, [rag_label, mcp_label])) or "LLM only"
        logger.info(f"Registered agent: {agent.name} [{capabilities}]")
        orchestrator.register(agent)

    # ── Hot-reload: register every agent with PromptLoader so prompt edits
    # on disk are picked up within POLL_INTERVAL_S without a PM2 restart.
    # Also enables /admin/reload-prompts for instant manual refresh.
    loader = PromptLoader(prompts_dir=prompts_dir)
    for agent in agents:
        loader.register(agent.name, agent)
    set_loader(loader)
    orchestrator.prompt_loader = loader  # type: ignore[attr-defined]

    return orchestrator
