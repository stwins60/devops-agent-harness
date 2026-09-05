"""Observability specialist: correlates metrics + logs + alerts + deployments + infrastructure changes."""
from __future__ import annotations

from typing import Optional

from agent.models import Diagnosis, Hypothesis, Plan
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class ObservabilitySpecialist(Specialist):
    name = "observability-agent"
    description = "Queries Prometheus/Loki/alerts and correlates them with deployments to find regressions."
    domains = ["observability", "incident"]
    keywords = ["prometheus", "grafana", "loki", "metrics", "logs", "alert", "alerts", "latency", "error rate", "dashboard", "traces", "slow", "errors"]

    def investigate(self, inv: Investigation) -> None:
        service = inv.target("service") or inv.target("deployment")
        if not service:
            inv.log.fact("No service named in the request; skipping metric correlation.", source="observability-agent")
            return
        ns = inv.target("namespace") or self.h.config.default_namespace
        health = self.call(inv, "obs_service_health", {"service": service, "namespace": ns}, purpose=f"health snapshot of {service}")
        if not health.ok:
            return
        m = health.output.get("metrics", {})
        err, p95, avail, restarts, up = m.get("error_rate"), m.get("p95_latency_s"), m.get("available_replicas"), m.get("container_restarts"), m.get("up")
        inv.log.fact(f"Metrics for {service}: 5xx ratio {_fmt(err, pct=True)}, p95 latency {_fmt(p95)}s, available replicas {_fmt(avail)}, container restarts {_fmt(restarts)}, up={_fmt(up)}.",
                     source="obs_service_health", error_rate=err, p95=p95, available=avail, restarts=restarts, up=up, service=service)
        alerts = health.output.get("alerts", [])
        if alerts:
            inv.log.fact("Firing alerts: " + "; ".join(f"{a.get('name')} ({a.get('severity')}) since {a.get('since')}: {a.get('summary')}" for a in alerts), source="obs_alerts", alerts=alerts)
        deploys = sorted(health.output.get("deployments", []), key=lambda d: d.get("time", ""))
        if deploys:
            last = deploys[-1]
            inv.log.fact(f"Most recent deployment of {service}: version {last.get('version')} at {last.get('time')} by {last.get('by')}{' (commit ' + last['commit'] + ')' if last.get('commit') else ''}.",
                         source="obs_deployment_timeline", last_deploy=last, deployments=deploys[-3:])
            inv.task.links.deployment = f"{service}@{last.get('version')} {last.get('time')}"
        logs = self.call(inv, "obs_loki_query", {"query": f'{{namespace="{ns}",app="{service}"}}', "minutes": 30, "limit": 50}, purpose="recent application logs")
        if logs.ok and logs.output.get("lines"):
            lines = logs.output["lines"]
            inv.log.fact(f"Recent log lines ({len(lines)}): " + " | ".join(lines[-4:]), source="obs_loki_query", loki_lines=lines[-10:])

    def analyzers(self):
        return [("obs.deploy_correlation", _deploy_correlation), ("obs.error_rate", _error_rate)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        return None


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        return f"{v * 100:.1f}%" if pct else (f"{v:.3f}" if isinstance(v, float) else str(v))
    return str(v)[:40]


def _deploy_correlation(log: EvidenceLog) -> list[Hypothesis]:
    last = log.get("last_deploy")
    alerts = log.get("alerts") or []
    if not last or not alerts:
        return []
    first_alert = min((a.get("since") or "" for a in alerts), default="")
    if last.get("time") and first_alert and last["time"] <= first_alert:
        log.inference(f"Deployment {last.get('version')} at {last.get('time')} precedes the first alert at {first_alert}; the regression correlates with that deployment.", confidence=0.85)
        log.recommendation(f"Mitigate by rolling back {log.get('service')} to the previous revision (kubectl rollout undo) while the fix is prepared.")
        confirmed = log.has("probe_port") or log.has("last_reason", "OOMKilled") or log.get("unavailable")
        return [Hypothesis(statement=f"The {last.get('version')} deployment at {last.get('time')} introduced the regression that triggered {[a.get('name') for a in alerts]}.",
                           validation="Workload evidence (pods/probes/logs) identifies a fault introduced by the new revision; rollback restores health.",
                           status="confirmed" if confirmed else "unvalidated", confidence=0.9 if confirmed else 0.6)]
    return []


def _error_rate(log: EvidenceLog) -> list[Hypothesis]:
    err, avail = log.get("error_rate"), log.get("available")
    if isinstance(err, (int, float)) and err > 0.05:
        cause = "no replicas are available" if avail in (0, 0.0) else "the service is degraded"
        return [Hypothesis(statement=f"5xx ratio is {err * 100:.0f}% because {cause}.", validation="available replica count and endpoint state.", status="confirmed" if avail in (0, 0.0) else "unvalidated",
                           confidence=0.9 if avail in (0, 0.0) else 0.5)]
    return []
