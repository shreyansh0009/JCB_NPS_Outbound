"""
MCPClient: HTTP client for Model Context Protocol tool servers.

MCP servers expose tools over HTTP (JSON-RPC 2.0).

Discovery endpoint:  GET  /tools              → list of tool definitions
Call endpoint:       POST /tools/call          → call a tool by name

This client also supports a simpler REST convention used by many internal
endpoints (not full JSON-RPC): POST /{tool_name} with a JSON body.

Configure which convention to use per-server via `protocol`:
  "mcp"   — official MCP JSON-RPC (default)
  "rest"  — simple REST (POST /{tool_name})

Env vars:
  MCP_TIMEOUT  - default: 10 (seconds)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict        # JSON Schema of input parameters
    server_name: str        # which server this tool belongs to


@dataclass
class MCPToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str = ""


class MCPClient:
    """
    Client for a single MCP server.

    server_url:  base URL, e.g. "https://api.example.com"
    protocol:    "mcp" | "rest"
    headers:     auth headers (e.g. {"X-API-Key": "..."})
    """
    def __init__(
        self,
        server_name: str,
        server_url: str,
        protocol: str = "mcp",
        headers: dict[str, str] | None = None,
        timeout: int = 10,
    ):
        self.server_name = server_name
        self.server_url = server_url.rstrip("/")
        self.protocol = protocol
        self.headers = headers or {}
        self.timeout = timeout
        self._tools: dict[str, MCPTool] = {}

    async def discover_tools(self) -> list[MCPTool]:
        """Fetch the list of tools this server exposes."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                if self.protocol == "mcp":
                    # JSON-RPC 2.0: tools/list
                    resp = await client.post(
                        f"{self.server_url}",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    )
                    resp.raise_for_status()
                    tools_data = resp.json().get("result", {}).get("tools", [])
                else:
                    # Simple REST discovery
                    resp = await client.get(f"{self.server_url}/tools")
                    resp.raise_for_status()
                    tools_data = resp.json().get("tools", [])

            tools = []
            for t in tools_data:
                tool = MCPTool(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t.get("inputSchema", t.get("parameters", {})),
                    server_name=self.server_name,
                )
                self._tools[tool.name] = tool
                tools.append(tool)

            logger.info(f"Discovered {len(tools)} tools from '{self.server_name}'")
            return tools

        except Exception as e:
            logger.warning(f"Could not discover tools from '{self.server_name}': {e}")
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolResult:
        """Call a specific tool on this server."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                if self.protocol == "mcp":
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    }
                    resp = await client.post(self.server_url, json=payload)
                    resp.raise_for_status()
                    result = resp.json().get("result", {})
                    # MCP returns content array; extract text
                    content = result.get("content", [])
                    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
                    return MCPToolResult(tool_name=tool_name, success=True, data=text or result)
                else:
                    # Simple REST
                    resp = await client.post(
                        f"{self.server_url}/{tool_name}", json=arguments
                    )
                    resp.raise_for_status()
                    return MCPToolResult(tool_name=tool_name, success=True, data=resp.json())

        except httpx.HTTPStatusError as e:
            logger.error(f"MCP tool '{tool_name}' HTTP error: {e.response.status_code}")
            return MCPToolResult(tool_name=tool_name, success=False, error=str(e))
        except Exception as e:
            logger.exception(f"MCP tool '{tool_name}' call failed")
            return MCPToolResult(tool_name=tool_name, success=False, error=str(e))
