"""Networking tools: DNS, TCP reachability, HTTP checks (socket/urllib based; no external binaries required)."""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld


class NetworkBackend(Protocol):
    def dns(self, host: str) -> list[str]: ...
    def tcp(self, host: str, port: int, timeout: float = 3.0) -> dict[str, Any]: ...
    def http(self, url: str, timeout: float = 5.0) -> dict[str, Any]: ...


class SocketNetworkBackend:
    def dns(self, host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ToolError(f"DNS resolution failed for {host}: {exc}", kind="network") from exc
        return sorted({i[4][0] for i in infos})

    def tcp(self, host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
        started = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"host": host, "port": port, "open": True, "latency_ms": round((time.time() - started) * 1000, 1)}
        except OSError as exc:
            return {"host": host, "port": port, "open": False, "error": str(exc)}

    def http(self, url: str, timeout: float = 5.0) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "devops-agent-harness/0.1"})
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {"url": url, "status": resp.status, "latency_ms": round((time.time() - started) * 1000, 1), "body": resp.read(200).decode("utf-8", "replace")}
        except urllib.error.HTTPError as exc:
            return {"url": url, "status": exc.code, "latency_ms": round((time.time() - started) * 1000, 1), "body": exc.reason}
        except urllib.error.URLError as exc:
            return {"url": url, "status": None, "error": str(exc.reason)}


class MockNetworkBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def dns(self, host):
        if host not in self.world.network["dns"]:
            raise ToolError(f"DNS resolution failed for {host}: [Errno -2] Name or service not known", kind="network")
        return list(self.world.network["dns"][host])

    def tcp(self, host, port, timeout=3.0):
        ok = self.world.network["tcp"].get((host, int(port)), False)
        return {"host": host, "port": port, "open": ok, "latency_ms": 2.1 if ok else None, "error": None if ok else "connection refused"}

    def http(self, url, timeout=5.0):
        status, body = self.world.network["http"].get(url, (None, "no route to host"))
        return {"url": url, "status": status, "latency_ms": 12.0 if status else None, "body": body, "error": None if status else body}


@tool("net_dns_lookup", "Resolve a hostname.", category="networking", permissions=["network.read"],
      input_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]})
def net_dns_lookup(args, ctx):
    return {"host": args["host"], "addresses": ctx.backend("network").dns(args["host"])}


@tool("net_tcp_check", "Check whether a TCP port is reachable.", category="networking", permissions=["network.read"],
      input_schema={"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}}, "required": ["host", "port"]})
def net_tcp_check(args, ctx):
    return ctx.backend("network").tcp(args["host"], int(args["port"]))


@tool("net_http_check", "Perform an HTTP GET and report status/latency.", category="networking", permissions=["network.read"],
      input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
def net_http_check(args, ctx):
    return ctx.backend("network").http(args["url"])


def build_tools() -> list[Tool]:
    return [net_dns_lookup, net_tcp_check, net_http_check]
