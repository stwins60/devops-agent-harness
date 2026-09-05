"""MCP stdio server exposing harness tools to external coding agents (Claude Code, OpenCode, Copilot, ...).

Every call still goes through the executor: policy, approval (non-interactive -> deny unless
pre-approved in config ``mcp_preapproved``), redaction and audit logging apply exactly as for the CLI.
"""
from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from agent.audit.redaction import redact
from agent.models import PermissionLevel, TaskKind, new_id

if TYPE_CHECKING:  # pragma: no cover
    from agent.harness import Harness

PROTOCOL_VERSION = "2024-11-05"


class HarnessMcpServer:
    def __init__(self, harness: "Harness", max_permission: PermissionLevel = PermissionLevel.DESTROY) -> None:
        self.h = harness
        self.max_permission = max_permission
        self.task = harness.store.create(f"mcp session {new_id('mcp')}", task_id=new_id("mcp"), kind=TaskKind.EXECUTE, mode=harness.config.mode,
                                         environment=harness.config.environment)
        from agent.context.environment import resolve_environment

        self.task.environment = resolve_environment(harness.config).environment

    # -- protocol ----------------------------------------------------------
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, params, req_id = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "initialize":
            return self._ok(req_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "devops-agent-harness", "version": "0.1.0"}})
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            defs = self.h.registry.model_tool_definitions(max_permission=self.max_permission)
            return self._ok(req_id, {"tools": [{"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]} for d in defs]})
        if method == "tools/call":
            name, args = str(params.get("name")), dict(params.get("arguments") or {})
            result = self.h.executor.run(name, args, self.task, agent="mcp-client", purpose="requested via MCP")
            payload = redact(result.output) if result.ok else {"error": result.error, "kind": result.failure_kind, "advice": result.advice}
            return self._ok(req_id, {"content": [{"type": "text", "text": json.dumps(payload, default=str)}], "isError": not result.ok})
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def serve_forever(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                response = self.handle(msg)
            except Exception as exc:  # noqa: BLE001
                response = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(exc)}}
            if response is not None:
                stdout.write(json.dumps(response, default=str) + "\n")
                stdout.flush()
        return 0


def serve(harness: "Harness") -> int:
    return HarnessMcpServer(harness).serve_forever()
