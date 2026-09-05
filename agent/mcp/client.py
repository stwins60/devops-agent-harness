"""Minimal MCP (Model Context Protocol) stdio client.

Speaks JSON-RPC 2.0 over a child process' stdin/stdout, enough to
``initialize``, ``tools/list`` and ``tools/call``. No third-party dependency.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from tools.base import ToolError
from tools.shell import child_environment

PROTOCOL_VERSION = "2024-11-05"


class McpClient:
    def __init__(self, command: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None,
                 name: str = "mcp", timeout: int = 60) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env or {}
        self.name = name
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen[str]] = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self._tools_cache: Optional[list[dict[str, Any]]] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "McpClient":
        if self._proc is not None:
            return self
        env = child_environment()
        env.update(self.env)
        try:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, encoding="utf-8", cwd=str(self.cwd) if self.cwd else None, env=env,
                                          bufsize=1)
        except FileNotFoundError as exc:
            raise ToolError(f"MCP server command not found: {self.command[0]}", kind="unavailable") from exc
        result = self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                            "clientInfo": {"name": "devops-agent-harness", "version": "0.1.0"}})
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._notify("notifications/initialized", {})
        return self

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # pragma: no cover - best effort
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def __enter__(self) -> "McpClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- protocol --------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self._proc is None:
            self.start()
        assert self._proc and self._proc.stdout
        with self._lock:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            timer = threading.Timer(self.timeout, self._proc.kill)
            timer.start()
            try:
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        stderr = self._proc.stderr.read() if self._proc.stderr else ""
                        raise ToolError(f"MCP server '{self.name}' closed the connection: {stderr[:300]}", kind="network")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # ignore non-JSON noise on stdout
                    if msg.get("id") != req_id:
                        continue  # notifications / other ids
                    if "error" in msg:
                        err = msg["error"]
                        raise ToolError(f"MCP error {err.get('code')}: {err.get('message')}", kind="invalid")
                    return msg.get("result")
            finally:
                timer.cancel()

    # -- tools -----------------------------------------------------------
    def list_tools(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools_cache is None or refresh:
            result = self._call("tools/list", {})
            self._tools_cache = list(result.get("tools", [])) if isinstance(result, dict) else []
        return self._tools_cache

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict):
            if result.get("isError"):
                text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
                raise ToolError(f"MCP tool '{name}' failed: {text}", kind="unknown")
            content = result.get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except (json.JSONDecodeError, TypeError):
                    return joined
        return result


def client_from_config(spec: dict[str, Any], cwd: Optional[Path] = None) -> McpClient:
    """Build a client from a config entry: {name, command: [..], env: {...}, cwd}."""
    command = spec.get("command")
    if isinstance(command, str):
        command = command.split()
    if not command:
        raise ValueError(f"MCP server '{spec.get('name')}' has no command")
    env = {}
    for k, v in (spec.get("env") or {}).items():
        # allow ${VAR} indirection so tokens stay out of config files
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            v = os.environ.get(v[2:-1], "")
        env[k] = str(v)
    return McpClient(list(command), cwd=Path(spec["cwd"]) if spec.get("cwd") else cwd, env=env,
                     name=str(spec.get("name", command[0])), timeout=int(spec.get("timeout", 60)))
