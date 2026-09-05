"""Observability tools: Prometheus / Alertmanager / Loki HTTP APIs + deployment timeline + correlation."""
from __future__ import annotations

import time
from typing import Any, Optional, Protocol

from tools.base import Tool, ToolContext, ToolError, tool
from tools.http import HttpClient
from tools.mock.world import MockWorld


class ObservabilityBackend(Protocol):
    def prometheus_query(self, query: str) -> Any: ...
    def alerts(self) -> list[dict[str, Any]]: ...
    def loki_query(self, query: str, minutes: int = 30, limit: int = 100) -> list[str]: ...
    def deployments(self, service: Optional[str] = None) -> list[dict[str, Any]]: ...


class HttpObservabilityBackend:
    def __init__(self, prometheus_url: Optional[str], loki_url: Optional[str] = None, alertmanager_url: Optional[str] = None) -> None:
        self.prom = HttpClient(prometheus_url) if prometheus_url else None
        self.loki = HttpClient(loki_url) if loki_url else None
        self.alertmanager = HttpClient(alertmanager_url) if alertmanager_url else None

    def prometheus_query(self, query: str) -> Any:
        if not self.prom:
            raise ToolError("prometheus_url is not configured", kind="unavailable")
        data = self.prom.get("/api/v1/query", params={"query": query})
        results = (data or {}).get("data", {}).get("result", [])
        if not results:
            return None
        if len(results) == 1 and "value" in results[0]:
            try:
                return float(results[0]["value"][1])
            except (TypeError, ValueError, IndexError):
                return results[0]["value"]
        return [{"metric": r.get("metric"), "value": r.get("value", [None, None])[1]} for r in results]

    def alerts(self) -> list[dict[str, Any]]:
        if self.alertmanager:
            data = self.alertmanager.get("/api/v2/alerts")
            return [{"name": a.get("labels", {}).get("alertname"), "severity": a.get("labels", {}).get("severity"), "since": a.get("startsAt"),
                     "summary": a.get("annotations", {}).get("summary")} for a in data or []]
        if self.prom:
            data = self.prom.get("/api/v1/alerts")
            return [{"name": a.get("labels", {}).get("alertname"), "severity": a.get("labels", {}).get("severity"), "since": a.get("activeAt"),
                     "summary": a.get("annotations", {}).get("summary"), "state": a.get("state")} for a in (data or {}).get("data", {}).get("alerts", [])]
        raise ToolError("no alert source configured", kind="unavailable")

    def loki_query(self, query: str, minutes: int = 30, limit: int = 100) -> list[str]:
        if not self.loki:
            raise ToolError("loki_url is not configured", kind="unavailable")
        now = time.time_ns()
        data = self.loki.get("/loki/api/v1/query_range", params={"query": query, "start": now - minutes * 60 * 10**9, "end": now, "limit": limit})
        lines = []
        for stream in (data or {}).get("data", {}).get("result", []):
            for _, line in stream.get("values", []):
                lines.append(line)
        return lines

    def deployments(self, service: Optional[str] = None) -> list[dict[str, Any]]:
        return []  # real deployment timeline comes from kubectl rollout history / ArgoCD; see specialists


class MockObservabilityBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def prometheus_query(self, query: str) -> Any:
        series = self.world.observability["prometheus"]
        if query in series:
            return series[query]
        for k, v in series.items():
            if query.replace(" ", "") == k.replace(" ", ""):
                return v
        return None

    def alerts(self):
        return list(self.world.observability["alerts"])

    def loki_query(self, query, minutes=30, limit=100):
        return list(self.world.observability["loki"].get(query, []))[:limit]

    def deployments(self, service=None):
        return [d for d in self.world.observability["deployments"] if not service or d["service"] == service]


@tool("obs_prometheus_query", "Run an instant PromQL query.", category="observability", permissions=["observability.read"],
      input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
def obs_prometheus_query(args, ctx):
    return {"query": args["query"], "value": ctx.backend("observability").prometheus_query(args["query"])}


@tool("obs_alerts", "List firing alerts.", category="observability", permissions=["observability.read"])
def obs_alerts(args, ctx):
    return {"alerts": ctx.backend("observability").alerts()}


@tool("obs_loki_query", "Query logs from Loki (LogQL).", category="observability", permissions=["observability.read"],
      input_schema={"type": "object", "properties": {"query": {"type": "string"}, "minutes": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["query"]})
def obs_loki_query(args, ctx):
    return {"lines": ctx.backend("observability").loki_query(args["query"], int(args.get("minutes") or 30), int(args.get("limit") or 100))}


@tool("obs_deployment_timeline", "Recent deployments for a service (from the deployment tracker / GitOps).", category="observability", permissions=["observability.read"],
      input_schema={"type": "object", "properties": {"service": {"type": "string"}}})
def obs_deployment_timeline(args, ctx):
    return {"deployments": ctx.backend("observability").deployments(args.get("service"))}


@tool("obs_service_health", "Composite health snapshot for a service: error rate, p95 latency, availability, restarts, alerts and deployments.",
      category="observability", permissions=["observability.read"],
      input_schema={"type": "object", "properties": {"service": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["service"]})
def obs_service_health(args, ctx):
    be = ctx.backend("observability")
    svc, ns = args["service"], args.get("namespace") or "production"
    queries = {
        "error_rate": f'sum(rate(http_requests_total{{job="{svc}",code=~"5.."}}[5m])) / sum(rate(http_requests_total{{job="{svc}"}}[5m]))',
        "p95_latency_s": f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{job="{svc}"}}[5m])) by (le))',
        "available_replicas": f'kube_deployment_status_replicas_available{{deployment="{svc}",namespace="{ns}"}}',
        "container_restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{ns}",container="{svc}"}})',
        "memory_working_set_bytes": f'container_memory_working_set_bytes{{container="{svc}",namespace="{ns}"}}',
        "up": f'up{{job="{svc}"}}',
    }
    metrics: dict[str, Any] = {}
    for name, q in queries.items():
        try:
            metrics[name] = be.prometheus_query(q)
        except ToolError as exc:
            metrics[name] = f"unavailable: {exc}"
    return {"service": svc, "metrics": metrics, "alerts": be.alerts(), "deployments": be.deployments(svc)}


def build_tools() -> list[Tool]:
    return [obs_prometheus_query, obs_alerts, obs_loki_query, obs_deployment_timeline, obs_service_health]
