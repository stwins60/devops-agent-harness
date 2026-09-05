"""Tool adapters: Native / CLI / MCP / REST / SDK.

These wrap heterogeneous capability sources behind the ``Tool`` interface so
the executor, policy engine and specialists never care where a capability
comes from.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent.mcp.client import McpClient
from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError
from tools.http import HttpClient
from tools.shell import run_command


class CliTool(Tool):
    """Runs a shell command supplied at call time.

    The executor classifies the command (SAFE/CAUTION/DANGEROUS/FORBIDDEN) before
    this tool ever runs; the classification decides permission and approval.
    """

    spec = ToolSpec(
        name="shell_run",
        description="Run a shell command in the workspace. The command is classified by the safety layer; "
                    "dangerous commands need explicit approval and forbidden commands are refused.",
        risk_level=RiskLevel.MEDIUM, requires_approval=False, permission=PermissionLevel.READ,
        permissions=["shell.execute"], timeout=300, category="linux",
        input_schema={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"},
                                                       "timeout": {"type": "integer"}}, "required": ["command"]},
        output_schema={"type": "object", "properties": {"returncode": {"type": "integer"}, "stdout": {"type": "string"},
                                                        "stderr": {"type": "string"}}},
        tags=["cli"],
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        cwd = Path(args["cwd"]) if args.get("cwd") else (ctx.workspace or ctx.project_root)
        timeout = int(args.get("timeout") or ctx.timeout or self.spec.timeout)
        if ctx.mock and ctx.backends.get("linux") is not None:
            out = ctx.backends["linux"].run(command)
        else:
            out = run_command(command, cwd=cwd, timeout=timeout, shell=True)
        result = ToolResult(ok=out.ok, output=out.to_dict(), tool=self.name, args=args, duration=out.duration)
        if out.timed_out:
            result.error = f"command timed out after {timeout}s"
            result.failure_kind = "timeout"
        elif not out.ok:
            result.error = (out.stderr or out.stdout or f"exit code {out.returncode}").strip()[:2000]
        return result


class McpTool(Tool):
    """Exposes a remote MCP tool as a harness tool.

    Because MCP servers do not declare risk, the permission/risk of each tool
    comes from the harness configuration (``mcp_servers[].tools[<name>]``).
    Unknown MCP tools default to MODIFY + requires_approval so nothing external
    runs silently.
    """

    def __init__(self, client: McpClient, remote_name: str, remote_description: str, input_schema: dict[str, Any],
                 *, server_name: str, permission: PermissionLevel = PermissionLevel.MODIFY, risk: RiskLevel = RiskLevel.MEDIUM,
                 requires_approval: bool = True, category: str = "mcp", timeout: int = 60) -> None:
        spec = ToolSpec(
            name=f"mcp_{server_name}_{remote_name}".replace("-", "_").replace(".", "_"),
            description=f"[MCP:{server_name}] {remote_description}",
            risk_level=risk, requires_approval=requires_approval, permission=permission,
            permissions=[f"mcp.{server_name}.{remote_name}"], input_schema=input_schema or {"type": "object"},
            timeout=timeout, category=category, mutating=permission >= PermissionLevel.MODIFY, tags=["mcp", server_name],
        )
        super().__init__(spec)
        self.client = client
        self.remote_name = remote_name

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.time()
        output = self.client.call_tool(self.remote_name, args)
        return ToolResult(ok=True, output=output, tool=self.name, args=args, duration=time.time() - started)


def tools_from_mcp_server(client: McpClient, server_config: dict[str, Any]) -> list[McpTool]:
    """Discover tools from an MCP server and wrap them with configured risk metadata."""
    server_name = str(server_config.get("name") or client.name)
    overrides: dict[str, dict[str, Any]] = server_config.get("tools") or {}
    default_permission = PermissionLevel.parse(server_config.get("default_permission", "MODIFY"))
    read_prefixes = tuple(server_config.get("read_only_prefixes") or ("get", "list", "read", "search", "describe", "query", "fetch"))
    out: list[McpTool] = []
    for t in client.list_tools():
        name = t.get("name", "")
        cfg = overrides.get(name, {})
        if cfg.get("disabled"):
            continue
        if "permission" in cfg:
            permission = PermissionLevel.parse(cfg["permission"])
        elif name.lower().startswith(read_prefixes):
            permission = PermissionLevel.READ
        else:
            permission = default_permission
        requires_approval = bool(cfg.get("requires_approval", permission >= PermissionLevel.MODIFY))
        risk = RiskLevel.parse(cfg.get("risk_level", "low" if permission <= PermissionLevel.ANALYZE else "medium"))
        out.append(McpTool(client, name, t.get("description", ""), t.get("inputSchema") or {}, server_name=server_name,
                           permission=permission, risk=risk, requires_approval=requires_approval,
                           category=str(server_config.get("category", "mcp")), timeout=int(server_config.get("timeout", 60))))
    return out


class RestTool(Tool):
    """Generic REST call tool bound to a configured base URL and method."""

    def __init__(self, spec: ToolSpec, client: HttpClient, method: str, path_template: str,
                 body_builder: Optional[Callable[[dict[str, Any]], Any]] = None) -> None:
        super().__init__(spec)
        self.client = client
        self.method = method
        self.path_template = path_template
        self.body_builder = body_builder

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.time()
        try:
            path = self.path_template.format(**args)
        except KeyError as exc:
            raise ToolError(f"missing path argument {exc}", kind="invalid") from exc
        body = self.body_builder(args) if self.body_builder else args.get("body")
        output = self.client.request(self.method, path, body=body, params=args.get("params"))
        return ToolResult(ok=True, output=output, tool=self.name, args=args, duration=time.time() - started)


class SdkTool(Tool):
    """Wraps an SDK/python callable (e.g. boto3 client method) discovered at runtime."""

    def __init__(self, spec: ToolSpec, factory: Callable[[ToolContext], Callable[..., Any]]) -> None:
        super().__init__(spec)
        self.factory = factory

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.time()
        fn = self.factory(ctx)
        output = fn(**args)
        return ToolResult(ok=True, output=output, tool=self.name, args=args, duration=time.time() - started)
