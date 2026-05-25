"""
AgentRegistry — Godrej Appliances Customer Support Squad.
 
Handles:
  - Loading prompts from /prompts/*.md
  - Setting up RAG retrievers per agent (using ChromaDB + embeddings)
  - Setting up MCP registries per agent (pointing at external tool servers)
  - Registering all 6 Godrej agents with the orchestrator
 
Agents wired:
  hello     → greeting, language lock, name + mobile confirmation
  screener  → requirement discovery and routing
  service   → complaint, repair, installation, warranty, escalation  [RAG + MCP]
  sales     → product info, pricing, dealer location, new purchase    [RAG + MCP]
  scheduler → appointment booking, rescheduling, cancellation         [MCP]
  closer    → resolution confirmation, satisfaction check, end call
 
To add a new agent:
  1. Create agents/my_agent.py
  2. Add prompt to prompts/my_agent.md
  3. Register it in the agents list below
"""
from __future__ import annotations
 
import json
import logging
import os
from pathlib import Path
 
from core.orchestrator import Orchestrator
from agents.hello import HelloAgent
from agents.screener import ScreenerAgent
from agents.service import ServiceAgent
from agents.sales import SalesAgent
from agents.scheduler import SchedulerAgent
from agents.closer import CloserAgent
 
logger = logging.getLogger(__name__)
 
 
# ── Helpers (identical interface to original registry.py) ─────────────────────
 
def _load_prompt(name: str) -> str:
    path = Path(__file__).parent.parent / "prompts" / f"{name}.md"
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
 
    server_configs: list of dicts from mcp_servers.json or env
    tool_map: {tool_name: server_name} for manual registration (skips discovery)
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
def _load_persona() -> str:
    path = Path(__file__).parent.parent / "prompts" / "persona.md"
    if not path.exists():
        logger.warning("persona.md not found — agents will run without persona prefix")
        return ""
    return path.read_text(encoding="utf-8")
 
# ── Main entry point ──────────────────────────────────────────────────────────
 
def build_orchestrator(squad_path: str, llm) -> Orchestrator:
    """
    Build and return a fully-wired Orchestrator for Godrej Appliances.
    RAG and MCP are enabled if dependencies are installed and configured.
    """
    orchestrator = Orchestrator(squad_path)
    orchestrator.load_squad()
 
    # ── Embeddings (shared across all RAG retrievers) ──────────────────────
    embeddings = None
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "sentence_transformers")
    try:
        from providers.rag.embeddings import get_embeddings
        embeddings = get_embeddings()
        logger.info(f"Embeddings provider: {embedding_provider}")
    except Exception as e:
        logger.warning(f"Embeddings not available ({e}) — RAG disabled")
 
    # ── RAG: Godrej Appliances collections ────────────────────────────────
    # Both collections are ingested from godrej_appliances_kb.md
    # Run: python ingest_kb.py  (once, before starting the server)
    service_rag = _build_rag("godrej_appliances_service", embeddings) if embeddings else None
    sales_rag   = _build_rag("godrej_appliances_products", embeddings) if embeddings else None
 
    # ── MCP: Godrej DMS tools ─────────────────────────────────────────────
    # Real MCP: loaded from config/mcp_servers.json (Salesforce DMS endpoint).
    # Mock MCP: used when no server is configured OR MCP_MOCK=true in .env.
    #           Returns realistic fake responses so the full booking/complaint
    #           flow works end-to-end during development.
    #           To switch to real: add mcp_servers.json and remove MCP_MOCK.
    mcp_servers = _load_mcp_config()
    use_mock = os.getenv("MCP_MOCK", "false").lower() in ("true", "1", "yes")

    if mcp_servers and not use_mock:
        dms_tools = {
            "get_service_slots":        "dms",
            "book_service_appointment": "dms",
            "register_complaint":       "dms",
            "check_warranty":           "dms",
            "get_inventory":            "dms",
        }
        dms_mcp = _build_mcp_registry(mcp_servers, dms_tools)
        if dms_mcp:
            logger.info(f"DMS MCP tools registered (real): {dms_mcp.list_tools()}")
    else:
        from providers.mcp.mock_client import MockMCPRegistry
        dms_mcp = MockMCPRegistry()
        logger.info(
            f"DMS MCP running in MOCK mode — fake responses for: {dms_mcp.list_tools()}. "
            f"Set MCP_MOCK=false and configure mcp_servers.json to use real Salesforce DMS."
        )

    persona = _load_persona()
 
    # ── Wire Godrej Appliances agents ──────────────────────────────────────
    agents = [
        HelloAgent(
            llm=llm,
            prompt=_load_prompt("hello"),
            persona=persona,  
            # No RAG or MCP — pure greeting and identity confirmation
        ),
        ScreenerAgent(
            llm=llm,
            prompt=_load_prompt("screener"),
            persona=persona,  
            # No RAG or MCP — routes based on conversation only
        ),
        ServiceAgent(
            llm=llm,
            prompt=_load_prompt("service"),
            persona=persona,  
            rag=service_rag,        # KB: warranty policy, service timelines, product categories
            mcp_registry=dms_mcp,  # DMS: register_complaint, check_warranty, get_service_slots
            rag_top_k=3,
        ),
        SalesAgent(
            llm=llm,
            prompt=_load_prompt("sales"),
            persona=persona,  
            rag=sales_rag,          # KB: product specs, categories, pricing guidance
            mcp_registry=dms_mcp,  # DMS: get_inventory
            rag_top_k=4,
        ),
        SchedulerAgent(
            llm=llm,
            prompt=_load_prompt("scheduler"),
            persona=persona,  
            mcp_registry=dms_mcp,  # DMS: get_service_slots, book_service_appointment
            # No RAG needed — purely slot management
        ),
        CloserAgent(
            llm=llm,
            prompt=_load_prompt("closer"),
            persona=persona,  
            # No RAG or MCP — resolution confirmation and call wrap-up only
        ),
    ]
 
    for agent in agents:
        rag_label = "RAG" if getattr(agent, "rag", None) else ""
        mcp_label = "MCP" if getattr(agent, "mcp_registry", None) else ""
        capabilities = ", ".join(filter(None, [rag_label, mcp_label])) or "LLM only"
        logger.info(f"Registered agent: {agent.name} [{capabilities}]")
        orchestrator.register(agent)
 
    return orchestrator