"""Generic MCP adapter: connect configured MCP servers and register their tools.

Config (``.agent/config.yaml``)::

    mcp_servers:
      - name: jira
        command: ["npx", "-y", "@example/mcp-jira"]
        env: { JIRA_TOKEN: "${JIRA_API_TOKEN}" }
        category: jira
        default_permission: MODIFY
        read_only_prefixes: [get, list, search]
        tools:
          create_issue: { permission: MODIFY, requires_approval: true, risk_level: medium }
          delete_issue: { disabled: true }
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent.mcp.client import McpClient, client_from_config
from tools.adapters import McpTool, tools_from_mcp_server
from tools.registry import ToolRegistry


class McpConnections:
    def __init__(self) -> None:
        self.clients: dict[str, McpClient] = {}
        self.errors: dict[str, str] = {}

    def connect_all(self, servers: list[dict[str, Any]], registry: ToolRegistry, cwd: Optional[Path] = None) -> list[McpTool]:
        registered: list[McpTool] = []
        for spec in servers or []:
            name = str(spec.get("name") or "mcp")
            try:
                client = client_from_config(spec, cwd=cwd).start()
                self.clients[name] = client
                tools = tools_from_mcp_server(client, spec)
                registry.register_all(tools, replace=True)
                registered.extend(tools)
            except Exception as exc:  # a broken MCP server must not break the harness
                self.errors[name] = str(exc)
        return registered

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.clients.clear()
