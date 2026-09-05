"""Linux host tools. Commands are predefined (no free-form shell here) and classified by risk."""
from __future__ import annotations

import os
import shlex
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import CommandOutput, run_command


class LinuxBackend(Protocol):
    def run(self, command: str, timeout: int = 60) -> CommandOutput: ...
    def hostname(self) -> str: ...


class LocalLinuxBackend:
    """Runs on the local host, or over SSH when ``host`` is given (uses the user's ssh agent/config)."""

    def __init__(self, host: Optional[str] = None) -> None:
        self.host = host

    def run(self, command: str, timeout: int = 60) -> CommandOutput:
        if self.host:
            return run_command(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, command], timeout=timeout,
                               env_passthrough=("SSH_AUTH_SOCK",))
        return run_command(command, timeout=timeout, shell=True)

    def hostname(self) -> str:
        return self.host or os.uname().nodename if hasattr(os, "uname") else (self.host or os.environ.get("COMPUTERNAME", "localhost"))


class MockLinuxBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, command: str, timeout: int = 60) -> CommandOutput:
        if self.world.flags.get("tool_timeout"):
            return CommandOutput(command, -1, "", "", float(timeout), timed_out=True)
        cmds = self.world.linux["commands"]
        norm = " ".join(command.split())
        if norm in cmds:
            return CommandOutput(command, 0, cmds[norm], "", 0.01)
        for key, out in cmds.items():
            if norm.startswith(key.split(" |")[0]) and key.split(" ")[0] == norm.split(" ")[0]:
                return CommandOutput(command, 0, out, "", 0.01)
        prog = norm.split(" ")[0]
        if prog in ("systemctl", "service") and any(w in norm for w in ("restart", "start", "stop")):
            self.world.record("service_restart", command=norm)
            return CommandOutput(command, 0, "", "", 0.2)
        if prog in ("ls", "cat", "stat", "head", "tail", "find"):
            return CommandOutput(command, 0, "", "", 0.01)
        return CommandOutput(command, 127, "", f"bash: {prog}: command not found (mock)", 0.01)

    def hostname(self) -> str:
        return self.world.linux["hostname"]


def _run(ctx: ToolContext, command: str, timeout: int = 60) -> dict[str, Any]:
    out = ctx.backend("linux").run(command, timeout=timeout)
    if out.timed_out:
        raise ToolError(f"command timed out after {timeout}s: {command}", kind="timeout")
    if out.returncode != 0 and not out.stdout:
        raise ToolError(f"{command}: exit {out.returncode}: {out.stderr.strip()[:400]}", kind="unknown")
    return {"command": command, "returncode": out.returncode, "stdout": out.stdout, "stderr": out.stderr}


def _read_tool(name: str, description: str, command_fn, props: Optional[dict[str, Any]] = None, required: Optional[list[str]] = None, timeout: int = 60):
    schema = {"type": "object", "properties": props or {}, "required": required or []}
    return tool(name, description, category="linux", permissions=["linux.read"], input_schema=schema, timeout=timeout)(
        lambda args, ctx: _run(ctx, command_fn(args), timeout))


def _safe_name(value: str) -> str:
    v = str(value).strip()
    if not v or any(ch in v for ch in ";&|`$\n"):
        raise ToolError(f"unsafe argument '{value}'", kind="invalid")
    return shlex.quote(v) if " " in v else v


linux_uptime = _read_tool("linux_uptime", "Uptime and load average.", lambda a: "uptime")
linux_disk_usage = _read_tool("linux_disk_usage", "Filesystem usage (df -h).", lambda a: "df -h")
linux_memory = _read_tool("linux_memory", "Memory usage (free -m).", lambda a: "free -m")
linux_top_processes = _read_tool("linux_top_processes", "Top processes by memory.", lambda a: "ps aux --sort=-%mem | head -n 10")
linux_service_status = _read_tool("linux_service_status", "systemctl status for a unit.", lambda a: f"systemctl status {_safe_name(a['unit'])}",
                                  {"unit": {"type": "string"}}, ["unit"])
linux_failed_units = _read_tool("linux_failed_units", "List failed systemd units.", lambda a: "systemctl --failed")
linux_journal = _read_tool("linux_journal", "Recent journal entries for a unit.", lambda a: f"journalctl -u {_safe_name(a['unit'])} -n {int(a.get('lines') or 50)} --no-pager",
                           {"unit": {"type": "string"}, "lines": {"type": "integer"}}, ["unit"])
linux_listening_ports = _read_tool("linux_listening_ports", "Listening sockets (ss -tulpn).", lambda a: "ss -tulpn")
linux_interfaces = _read_tool("linux_interfaces", "Network interfaces and addresses.", lambda a: "ip addr")
linux_routes = _read_tool("linux_routes", "Routing table.", lambda a: "ip route")
linux_dmesg = _read_tool("linux_dmesg", "Recent kernel messages.", lambda a: "dmesg -T | tail -n 50")
linux_os_release = _read_tool("linux_os_release", "OS release information.", lambda a: "cat /etc/os-release")
linux_dir_usage = _read_tool("linux_dir_usage", "Largest directories under a path.", lambda a: f"du -sh {_safe_name(a.get('path') or '/var')}/* | sort -rh | head -n 10",
                             {"path": {"type": "string"}}, [], timeout=180)
linux_largest_files = _read_tool("linux_largest_files", "Largest files in a directory.", lambda a: f"ls -lS {_safe_name(a.get('path') or '/var/log')} | head -n 10",
                                 {"path": {"type": "string"}})


class ServiceRestartTool(Tool):
    spec = ToolSpec(name="linux_service_restart", description="Restart a systemd unit.", permission=PermissionLevel.DEPLOY, risk_level=RiskLevel.HIGH,
                    requires_approval=True, permissions=["linux.service"], category="linux", mutating=True, timeout=120,
                    rollback="not reversible: a restart cannot be undone; verify service health after restart",
                    input_schema={"type": "object", "properties": {"unit": {"type": "string"}}, "required": ["unit"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=_run(ctx, f"systemctl restart {_safe_name(args['unit'])}", 120), tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [linux_uptime, linux_disk_usage, linux_memory, linux_top_processes, linux_service_status, linux_failed_units, linux_journal,
            linux_listening_ports, linux_interfaces, linux_routes, linux_dmesg, linux_os_release, linux_dir_usage, linux_largest_files, ServiceRestartTool()]
