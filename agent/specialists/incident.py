"""Incident response specialist.

ALERT -> TRIAGE -> SEVERITY -> EVIDENCE COLLECTION -> HYPOTHESES -> VALIDATION -> MITIGATION -> RECOVERY -> VERIFICATION -> POSTMORTEM
"""
from __future__ import annotations

import re
from typing import Any, Optional

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel, ValidationResult, now_iso
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class IncidentSpecialist(Specialist):
    name = "incident-agent"
    description = "Runs structured incident response: triage, severity, evidence, hypotheses, mitigation and postmortem artifacts."
    domains = ["incident"]
    keywords = ["incident", "outage", "down", "503", "500", "p1", "sev1", "sev2", "degraded", "returning", "on-call", "oncall"]

    def investigate(self, inv: Investigation) -> None:
        severity = self.triage(inv)
        inv.targets["severity"] = severity
        inv.targets.setdefault("timeline", []).append(f"{now_iso()} incident task opened: {inv.task.request}")
        self.use_runbook(inv, inv.task.request, domain=None)
        service = inv.target("service") or inv.target("deployment")
        if service:
            inv.set_target("deployment", service)
        # evidence collection delegates to domain specialists sharing the same evidence log
        for name in ("observability-agent", "kubernetes-agent", "networking-agent"):
            sp = self.h.specialists.get(name)
            if sp:
                sp.investigate(inv)
                if inv.blocked:
                    break
        inv.targets["timeline"].append(f"{now_iso()} evidence collected ({len(inv.log.facts())} facts)")

    @staticmethod
    def triage(inv: Investigation) -> str:
        low = inv.task.request.lower()
        prod = inv.task.environment.value in ("production", "unknown") or "production" in low or "prod" in low
        outage = bool(re.search(r"\b(down|outage|503|502|500|unavailable|not responding)\b", low))
        degraded = bool(re.search(r"\b(latency|slow|degraded|errors|error rate|timeouts?)\b", low))
        if prod and outage:
            sev = "SEV1"
        elif prod and degraded or outage:
            sev = "SEV2"
        elif degraded:
            sev = "SEV3"
        else:
            sev = "SEV4"
        inv.log.fact(f"Triage: severity {sev} (production={prod}, outage={outage}, degraded={degraded}).", source="incident-agent", severity=sev)
        return sev

    def analyzers(self):
        return [("incident.summary", _summary)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        service, ns = inv.target("deployment"), inv.target("namespace") or self.h.config.default_namespace
        plan = Plan(task_id=inv.task.id, title=f"Mitigate incident: {inv.task.request[:60]}", problem=inv.task.request, root_cause=diagnosis.conclusion or "not yet confirmed",
                    evidence=[f.statement for f in diagnosis.facts][:12], infrastructure=[f"Deployment {ns}/{service}"] if service else [],
                    validation=["kubectl rollout status reports success", "service endpoints > 0", "5xx ratio returns below 1%", "alerts resolve"],
                    required_permissions=["kubernetes.write"], risk_level=RiskLevel.HIGH)
        last = inv.log.get("last_deploy")
        if service and (last or inv.log.get("unavailable")):
            plan.changes.append(ProposedChange(description=f"Roll back deployment {ns}/{service} to the previous revision (mitigation)", kind="infrastructure",
                                               target=f"deployment/{service}", tool="kubectl_rollout_undo", args={"kind": "deployment", "name": service, "namespace": ns},
                                               risk=RiskLevel.HIGH, permission=PermissionLevel.DEPLOY, rollback=f"kubectl rollout undo deployment/{service} -n {ns} (returns to the current revision)",
                                               environment=inv.task.environment.value))
            plan.steps = ["approve rollback", "kubectl rollout undo", "verify endpoints and error rate", "prepare the permanent fix via the normal change flow", "write the postmortem"]
            plan.risks = ["rollback restores the previous version and its known limitations", "if the fault is not deployment-related the rollback will not help"]
            plan.rollback = [f"kubectl rollout undo deployment/{service} -n {ns} again to return to the current revision"]
        else:
            plan.steps = ["no automatic mitigation identified; escalate to the service owner with the evidence below"]
        return plan

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        if not any(c.applied for c in plan.changes):
            return results
        service, ns = inv.target("deployment"), inv.target("namespace") or self.h.config.default_namespace
        st = self.call(inv, "kubectl_rollout_status", {"kind": "deployment", "name": service, "namespace": ns}, purpose="verify rollback rollout")
        results.append(ValidationResult("rollout status", st.ok and "successfully" in str(st.output.get("status", "")), str(st.output.get("status") if st.ok else st.error)[:160]))
        ep = self.call(inv, "kubectl_get", {"kind": "endpoints", "name": service, "namespace": ns}, purpose="verify endpoints after mitigation")
        n = len([a for s in (ep.output.get("subsets", []) if ep.ok else []) for a in s.get("addresses", [])])
        results.append(ValidationResult("service endpoints populated", n > 0, f"{n} ready endpoint(s)"))
        inv.targets.setdefault("timeline", []).append(f"{now_iso()} mitigation applied and verified: endpoints={n}")
        return results

    def artifacts(self, inv: Investigation, diagnosis: Optional[Diagnosis]) -> dict[str, Any]:
        facts = inv.log.facts()
        contributing = [e.statement for e in inv.log.items if e.kind.value == "INFERENCE"]
        return {
            "severity": inv.target("severity", "SEV3"),
            "timeline": inv.target("timeline", []) + [f"{e.timestamp} {e.statement[:100]}" for e in facts if e.data.get("last_deploy") or e.data.get("alerts")],
            "impact": _impact(inv.log),
            "contributing": contributing[:6],
            "mitigation": [c.description + (" [applied]" if c.applied else " [proposed]") for c in (inv.task.plan.changes if inv.task.plan else [])],
            "corrective": [r for r in (diagnosis.recommendations if diagnosis else [])][:5],
            "preventive": ["add a manifest test asserting probe ports match container ports", "gate deployments on readiness of the new revision (progressive delivery)",
                           "alert on KubeDeploymentReplicasMismatch before user-facing 5xx"],
        }


def _impact(log: EvidenceLog) -> str:
    err, avail = log.get("error_rate"), log.get("available")
    parts = []
    if isinstance(err, (int, float)):
        parts.append(f"5xx ratio {err * 100:.0f}%")
    if avail is not None:
        parts.append(f"{avail} available replica(s)")
    if log.get("http_status"):
        parts.append(f"public endpoint returns HTTP {log.get('http_status')}")
    return "; ".join(parts) or "impact not quantified"


def _summary(log: EvidenceLog) -> list[Hypothesis]:
    return []
