"""Networking specialist: DNS -> TCP reachability -> HTTP status for the targets named in the request."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from agent.models import Diagnosis, Hypothesis, Plan
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class NetworkingSpecialist(Specialist):
    name = "networking-agent"
    description = "Checks DNS resolution, TCP reachability, HTTP responses and TLS for hosts and URLs."
    domains = ["networking"]
    keywords = ["dns", "latency", "timeout", "connection refused", "503", "502", "504", "tls", "ssl", "certificate", "port", "firewall", "load balancer", "unreachable", "network", "proxy"]

    def investigate(self, inv: Investigation) -> None:
        url = inv.target("url")
        host = inv.target("host")
        if not url and self.h.config.mock and inv.target("service"):
            url = f"https://{inv.target('service')}.example.com/healthz"
        if not url and not host:
            m = re.search(r"\b([a-z0-9\-]+\.(?:[a-z0-9\-]+\.)+[a-z]{2,})\b", inv.task.request.lower())
            host = m.group(1) if m else None
        if url:
            parsed = urlparse(url)
            host = host or parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        else:
            port = 443
        if not host:
            inv.log.fact("No host or URL to check in the request.", source="networking-agent")
            return
        dns = self.call(inv, "net_dns_lookup", {"host": host}, purpose=f"resolve {host}")
        if dns.ok:
            inv.log.fact(f"DNS {host} -> {dns.output.get('addresses')}", source=f"net_dns_lookup({host})", dns_ok=True, dns_host=host)
        else:
            inv.log.fact(f"DNS resolution of {host} failed: {dns.error}", source=f"net_dns_lookup({host})", dns_ok=False, dns_host=host)
            return
        tcp = self.call(inv, "net_tcp_check", {"host": host, "port": port}, purpose=f"tcp {host}:{port}")
        if tcp.ok:
            inv.log.fact(f"TCP {host}:{port} {'open' if tcp.output.get('open') else 'closed (' + str(tcp.output.get('error')) + ')'}", source="net_tcp_check",
                         tcp_open=tcp.output.get("open"), tcp_port=port)
        if url:
            http = self.call(inv, "net_http_check", {"url": url}, purpose=f"http {url}")
            if http.ok:
                inv.log.fact(f"HTTP GET {url} -> {http.output.get('status')} ({http.output.get('latency_ms')} ms) {str(http.output.get('body') or http.output.get('error') or '')[:60]}",
                             source="net_http_check", http_status=http.output.get("status"), http_url=url)

    def analyzers(self):
        return [("net.dns", _dns), ("net.tcp", _tcp), ("net.http", _http)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        return None


def _dns(log: EvidenceLog) -> list[Hypothesis]:
    if log.has("dns_ok", False):
        log.recommendation(f"Fix DNS for {log.get('dns_host')}: check the zone record, resolver configuration and search domains.")
        return [Hypothesis(statement=f"DNS resolution for {log.get('dns_host')} fails; clients cannot reach the service.", validation="getaddrinfo/dig failure.", status="confirmed", confidence=0.95)]
    return []


def _tcp(log: EvidenceLog) -> list[Hypothesis]:
    if log.has("tcp_open", False):
        return [Hypothesis(statement=f"TCP port {log.get('tcp_port')} on {log.get('dns_host')} is not reachable (closed/filtered).", validation="TCP connect refused/timeout.", status="confirmed", confidence=0.9)]
    return []


def _http(log: EvidenceLog) -> list[Hypothesis]:
    status = log.get("http_status")
    if status and status >= 500:
        backend_down = log.has("endpoint_count", 0) or log.get("unavailable")
        return [Hypothesis(statement=f"{log.get('http_url')} returns HTTP {status}: the edge is reachable but the backend is unavailable{' (service has no ready endpoints)' if backend_down else ''}.",
                           validation="Upstream/backend health: no ready endpoints or unhealthy targets.", status="confirmed" if backend_down else "unvalidated", confidence=0.9 if backend_down else 0.6)]
    return []
