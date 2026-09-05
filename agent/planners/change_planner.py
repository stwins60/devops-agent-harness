"""Change planner: builds a complete change plan (no mutations) for PLAN-kind requests.

Sources, in order of preference:
1. a matching runbook (structured prechecks/remediation/validation/rollback)
2. specialist proposals derived from evidence
3. a generic skeleton that explicitly states what is unknown
"""
from __future__ import annotations

from typing import Optional

from agent.models import Diagnosis, PermissionLevel, Plan, ProposedChange, RiskLevel
from agent.runbooks.loader import Runbook
from agent.specialists.base import Investigation


def build_change_plan(inv: Investigation, diagnosis: Optional[Diagnosis], proposals: list[Plan]) -> Plan:
    task = inv.task
    runbook = _best_runbook(inv)
    plan = Plan(task_id=task.id, title=task.request[:80], problem=task.request, root_cause=(diagnosis.conclusion if diagnosis and diagnosis.conclusion else "n/a (change request, not a fault)"),
                evidence=[f.statement for f in (diagnosis.facts if diagnosis else inv.log.facts())][:15])
    if runbook:
        _apply_runbook(plan, runbook, inv)
    for p in proposals:
        plan.changes.extend(c for c in p.changes if c.description not in {x.description for x in plan.changes})
        plan.files.extend(f for f in p.files if f not in plan.files)
        plan.infrastructure.extend(i for i in p.infrastructure if i not in plan.infrastructure)
        plan.risks.extend(r for r in p.risks if r not in plan.risks)
        plan.rollback.extend(r for r in p.rollback if r not in plan.rollback)
        plan.validation.extend(v for v in p.validation if v not in plan.validation)
        plan.cost_notes.extend(c for c in p.cost_notes if c not in plan.cost_notes)
        plan.required_permissions.extend(r for r in p.required_permissions if r not in plan.required_permissions)
        if p.risk_level.rank > plan.risk_level.rank:
            plan.risk_level = p.risk_level
    if not plan.changes and not plan.steps:
        plan.steps = ["gather the missing evidence listed under Risks", "draft the change with the service owner", "re-run `devops-agent plan` once inputs are known"]
        plan.risks.append("insufficient evidence to enumerate concrete changes; no runbook matched and no specialist proposed changes")
    _infra_from_evidence(plan, inv)
    if not plan.required_permissions:
        plan.required_permissions = sorted({c.permission.name for c in plan.changes} or {"READ"})
    if "upgrade" in task.request.lower() and not plan.cost_notes:
        plan.cost_notes.append("rolling node replacement temporarily runs extra capacity (surge nodes); no pricing data configured, so no monetary estimate is given")
    return plan


def _best_runbook(inv: Investigation) -> Optional[Runbook]:
    text = inv.task.request + " " + " ".join(inv.targets.get("domains", []))
    matches = inv.harness.runbooks.find(text, limit=1)
    if matches:
        rb = matches[0]
        if rb.name not in inv.runbooks_used:
            inv.runbooks_used.append(rb.name)
        return rb
    return None


def _apply_runbook(plan: Plan, rb: Runbook, inv: Investigation) -> None:
    plan.steps = [s.description for s in rb.prechecks] + [s.description for s in rb.remediation]
    plan.validation = [s.description for s in rb.validation]
    plan.rollback = [s.description for s in rb.rollback]
    plan.risks = [f"runbook severity: {rb.severity}"] + ([f"approval required by runbook '{rb.name}'"] if rb.approval_required else [])
    plan.risk_level = {"critical": RiskLevel.CRITICAL, "high": RiskLevel.HIGH, "medium": RiskLevel.MEDIUM}.get(rb.severity.lower(), RiskLevel.LOW)
    for step in rb.remediation:
        change = ProposedChange(description=step.description, kind="infrastructure" if not step.tool or step.tool.startswith(("kubectl", "aws", "terraform", "helm")) else "command",
                                target=step.tool or step.command or rb.domain, tool=step.tool, args=dict(step.args),
                                risk=plan.risk_level, permission=PermissionLevel.DEPLOY if (step.approval_required or rb.approval_required) else PermissionLevel.MODIFY,
                                rollback=plan.rollback[0] if plan.rollback else None, environment=inv.task.environment.value)
        for k, v in list(change.args.items()):
            if isinstance(v, str) and v.startswith("$"):
                change.args[k] = inv.target(v[1:], v)
        plan.changes.append(change)
    plan.infrastructure.append(f"runbook: {rb.name} ({rb.path.name})")


def _infra_from_evidence(plan: Plan, inv: Investigation) -> None:
    nodes = inv.log.get("nodes") or []
    if nodes:
        versions = inv.log.get("node_versions") or []
        plan.infrastructure.append(f"{len(nodes)} worker node(s) at kubelet {versions}")
    if inv.log.get("eks_version"):
        plan.infrastructure.append(f"EKS control plane {inv.log.get('eks_version')}, node group {inv.log.get('nodegroup')} at {inv.log.get('nodegroup_version')} (desired {inv.log.get('nodegroup_desired')})")
    if inv.log.get("tf_plan"):
        a = inv.log["tf_plan"] if isinstance(inv.log, dict) else inv.log.get("tf_plan")
        plan.infrastructure.append(f"terraform plan: +{a.get('add')} ~{a.get('change')} -{a.get('destroy')}")
