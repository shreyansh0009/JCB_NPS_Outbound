"""
MCPRegistry: manages multiple MCP servers and provides a unified interface
for agents to discover and call tools.

Usage:
  registry = MCPRegistry()
  registry.add_server("appointments", MCPClient(
      server_name="appointments",
      server_url="https://api.yourapp.com/mcp",
      protocol="mcp",
      headers={"X-API-Key": os.getenv("APPOINTMENTS_API_KEY")},
  ))
  await registry.discover_all()

  # In an agent:
  result = await registry.call("get_availability", {"date": "2025-01-16"})
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from providers.mcp.client import MCPClient, MCPTool, MCPToolResult

logger = logging.getLogger(__name__)


class MCPRegistry:
    def __init__(self):
        self._servers: dict[str, MCPClient] = {}
        self._tool_map: dict[str, MCPClient] = {}  # tool_name -> client

    def add_server(self, name: str, client: MCPClient) -> None:
        self._servers[name] = client
        logger.info(f"MCP server registered: {name} -> {client.server_url}")

    def add_server_from_config(self, config: dict) -> None:
        """
        Add a server from a dict config:
          {
            "name": "appointments",
            "url": "https://api.example.com",
            "protocol": "mcp",          # optional, default: "mcp"
            "api_key_env": "APPT_KEY",  # optional, reads from env
            "headers": {}               # optional, extra headers
          }
        """
        headers = dict(config.get("headers", {}))
        api_key_env = config.get("api_key_env")
        if api_key_env:
            key = os.environ.get(api_key_env, "")
            if key:
                headers["X-API-Key"] = key

        client = MCPClient(
            server_name=config["name"],
            server_url=config["url"],
            protocol=config.get("protocol", "mcp"),
            headers=headers,
            timeout=config.get("timeout", 10),
        )
        self.add_server(config["name"], client)

    async def discover_all(self) -> None:
        """Discover tools from all registered servers."""
        for name, client in self._servers.items():
            tools = await client.discover_tools()
            for tool in tools:
                if tool.name in self._tool_map:
                    logger.warning(
                        f"Tool '{tool.name}' already registered from "
                        f"'{self._tool_map[tool.name].server_name}', "
                        f"overwriting with '{name}'"
                    )
                self._tool_map[tool.name] = client

    def register_tool(self, tool_name: str, server_name: str) -> None:
        """Manually register a tool → server mapping (if discovery is not used)."""
        client = self._servers.get(server_name)
        if not client:
            raise ValueError(f"Server '{server_name}' not registered")
        self._tool_map[tool_name] = client

    async def call(self, tool_name: str, arguments: dict) -> MCPToolResult:
        """Call a tool by name. The registry routes it to the correct server."""
        from core.tracing import aspan, set_attr
        async with aspan("mcp.tool_call", attrs={"mcp.tool": tool_name}):
            client = self._tool_map.get(tool_name)
            if not client:
                set_attr("mcp.error", "no_server_registered")
                return MCPToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=f"No server registered for tool '{tool_name}'",
                )
            result = await client.call_tool(tool_name, arguments)
            set_attr("mcp.success", bool(result.success))
            return result

    def list_tools(self) -> list[str]:
        return list(self._tool_map.keys())

    def tool_descriptions_for_prompt(self) -> str:
        """
        Returns a formatted string describing all available MCP tools,
        suitable for injecting into an agent's system prompt.
        """
        if not self._tool_map:
            return ""
        lines = ["## Available MCP Tools\n"]
        seen = set()
        for tool_name, client in self._tool_map.items():
            if tool_name in seen:
                continue
            seen.add(tool_name)
            # Find the tool definition from the client
            tool = client._tools.get(tool_name)
            desc = tool.description if tool else ""
            lines.append(f"- `{tool_name}`: {desc}")
        lines.append(
            "\nCall a tool by including `[MCP:tool_name:{\"arg\": \"value\"}]` in your response."
        )
        return "\n".join(lines)
