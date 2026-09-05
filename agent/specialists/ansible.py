"""Ansible specialist: lint -> check mode -> review -> approval -> run -> validation."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class AnsibleSpecialist(Specialist):
    name = "ansible-agent"
    description = "Runs playbooks in check mode, reviews the diff and executes only after approval."
    domains = ["ansible"]
    keywords = ["ansible", "playbook", "inventory", "role", "vault", "idempotent"]

    def _find(self, inv: Investigation) -> tuple[Optional[str], Optional[str]]:
        repo = Path(inv.task.workspace) if inv.task.workspace else self.h.config.project_root
        playbooks = inv.target("ansible") or [str(p.relative_to(repo)) for p in repo.rglob("*.y*ml") if p.name in ("site.yml", "site.yaml", "playbook.yml", "playbook.yaml")]
        inventory = next((str(p.relative_to(repo)) for p in repo.rglob("*") if p.name in ("inventory", "inventory.ini", "hosts", "hosts.ini", "inventory.yml")), None)
        if self.h.config.mock and not playbooks:
            playbooks, inventory = ["site.yml"], inventory or "inventory"
        return (playbooks[0] if playbooks else None), inventory

    def investigate(self, inv: Investigation) -> None:
        playbook, inventory = self._find(inv)
        if not playbook:
            inv.log.fact("No Ansible playbook found in the workspace.", source="ansible-agent")
            return
        inv.set_target("playbook", playbook)
        inv.set_target("inventory", inventory or "inventory")
        lint = self.call(inv, "ansible_lint", {"playbook": playbook}, purpose="lint playbook")
        inv.log.fact(f"ansible-lint {playbook}: {'passed' if lint.ok else 'issues: ' + (lint.error or '')[:150]}", source="ansible_lint", ansible_lint_ok=lint.ok)
        chk = self.call(inv, "ansible_check", {"playbook": playbook, "inventory": inventory or "inventory"}, purpose="check mode (no changes)")
        if chk.ok:
            recap = chk.output.get("recap", {})
            changed = sum(v.get("changed", 0) for v in recap.values())
            failed = sum(v.get("failed", 0) + v.get("unreachable", 0) for v in recap.values())
            inv.log.fact(f"Check mode for {playbook}: {len(recap)} host(s), {changed} task(s) would change, {failed} failed/unreachable.", source="ansible_check",
                         ansible_changed=changed, ansible_failed=failed, ansible_hosts=list(recap))
        else:
            inv.log.fact(f"Check mode failed: {(chk.error or '')[:200]}", source="ansible_check", ansible_check_error=chk.error)

    def analyzers(self):
        return [("ansible.check", _check)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        if inv.log.get("ansible_changed") is None or inv.log.get("ansible_failed"):
            return None
        playbook, inventory = inv.target("playbook"), inv.target("inventory")
        plan = Plan(task_id=inv.task.id, title=f"Run playbook {playbook}", problem=inv.task.request, root_cause=diagnosis.conclusion or "",
                    evidence=[f.statement for f in diagnosis.facts][:6], infrastructure=[f"hosts: {inv.log.get('ansible_hosts')}"], risk_level=RiskLevel.MEDIUM,
                    risks=[f"{inv.log.get('ansible_changed')} task(s) change host state"], rollback=["re-run the playbook from the previous git revision (task reversibility varies)"],
                    validation=["ansible-playbook --check reports changed=0 afterwards", "service health checks"], required_permissions=["ansible.run"])
        plan.changes.append(ProposedChange(description=f"ansible-playbook {playbook} -i {inventory}", kind="infrastructure", target=playbook, tool="ansible_run",
                                           args={"playbook": playbook, "inventory": inventory}, risk=RiskLevel.MEDIUM, permission=PermissionLevel.DEPLOY,
                                           rollback="re-run previous playbook version", environment=inv.task.environment.value))
        return plan

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        if not any(c.applied and c.tool == "ansible_run" for c in plan.changes):
            return []
        chk = self.call(inv, "ansible_check", {"playbook": inv.target("playbook"), "inventory": inv.target("inventory")}, purpose="idempotency check after run")
        changed = sum(v.get("changed", 0) for v in (chk.output.get("recap", {}) if chk.ok else {}).values())
        return [ValidationResult("ansible idempotency (check mode after run)", chk.ok and changed == 0, f"{changed} task(s) would still change" if chk.ok else chk.error or "")]


def _check(log: EvidenceLog) -> list[Hypothesis]:
    if log.get("ansible_failed"):
        return [Hypothesis(statement="Playbook check mode reports failed or unreachable hosts.", validation="PLAY RECAP", status="confirmed", confidence=0.9)]
    if log.get("ansible_check_error"):
        return [Hypothesis(statement=f"Playbook cannot run: {log.get('ansible_check_error')[:120]}", validation="ansible output", status="confirmed", confidence=0.85)]
    if log.has("ansible_changed"):
        return [Hypothesis(statement=f"Playbook is runnable; {log.get('ansible_changed')} task(s) would change state.", validation="check mode recap", status="confirmed", confidence=0.85)]
    return []
