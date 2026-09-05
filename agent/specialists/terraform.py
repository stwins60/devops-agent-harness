"""Terraform specialist: inspect -> fmt -> validate -> plan -> risk analysis -> approval -> apply -> verify."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class TerraformSpecialist(Specialist):
    name = "terraform-agent"
    description = "Validates and plans Terraform changes, analyses plan risk, and applies only after approval."
    domains = ["terraform"]
    keywords = ["terraform", "tfstate", "tf", "module", "provider", "hcl", "plan", "apply"]

    def _dir(self, inv: Investigation) -> Optional[Path]:
        repo = Path(inv.task.workspace) if inv.task.workspace else self.h.config.project_root
        if inv.target("dir"):
            p = repo / inv.target("dir")
            return p if p.exists() else None
        tf = inv.target("terraform") or []
        if tf:
            return repo / Path(tf[0]).parent
        found = [p.parent for p in repo.rglob("*.tf") if ".terraform" not in p.parts]
        return sorted(found, key=lambda p: len(p.parts))[0] if found else None

    def investigate(self, inv: Investigation) -> None:
        d = self._dir(inv)
        if d is None:
            if self.h.config.mock:
                d = Path(inv.task.workspace or self.h.config.project_root)
            else:
                inv.log.fact("No Terraform configuration found in the workspace.", source="terraform-agent")
                return
        inv.set_target("terraform_dir", str(d))
        fmt = self.call(inv, "terraform_fmt_check", {"dir": str(d)}, purpose="formatting check")
        inv.log.fact(f"terraform fmt -check: {'clean' if fmt.ok else 'files need formatting: ' + (fmt.output or {}).get('stdout', '')[:100] if isinstance(fmt.output, dict) else fmt.error}",
                     source="terraform_fmt_check", tf_fmt_ok=fmt.ok)
        val = self.call(inv, "terraform_validate", {"dir": str(d)}, purpose="validate configuration")
        inv.log.fact(f"terraform validate: {'valid' if val.ok else 'INVALID - ' + (val.error or '')[:200]}", source="terraform_validate", tf_valid=val.ok, tf_validate_error=val.error)
        if not val.ok:
            return
        plan = self.call(inv, "terraform_plan", {"dir": str(d)}, purpose="execution plan")
        if not plan.ok:
            inv.log.fact(f"terraform plan failed: {(plan.error or '')[:200]}", source="terraform_plan", tf_plan_failed=True, tf_plan_error=plan.error)
            if plan.failure_kind == "auth":
                inv.blocked = "terraform plan needs cloud credentials"
            return
        a = plan.output.get("analysis", {})
        inv.log.fact(f"terraform plan: {a.get('add')} to add, {a.get('change')} to change, {a.get('destroy')} to destroy ({a.get('replaced')} replacements); risk {a.get('risk')}; "
                     f"resources: {[r['address'] + ' ' + r['action'] for r in a.get('resources', [])][:5]}", source="terraform_plan", tf_plan=a, tf_plan_text=plan.output.get("plan", "")[-3000:])

    def analyzers(self):
        return [("terraform.invalid", _invalid), ("terraform.plan_risk", _plan_risk)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        a = inv.log.get("tf_plan")
        d = inv.target("terraform_dir")
        if not a or a.get("no_changes"):
            return None
        risk = RiskLevel.parse(a.get("risk", "medium"))
        plan = Plan(task_id=inv.task.id, title=f"Apply Terraform plan in {d}", problem=inv.task.request, root_cause=diagnosis.conclusion or "planned infrastructure change",
                    evidence=[f.statement for f in diagnosis.facts][:8], infrastructure=[r["address"] for r in a.get("resources", [])], risk_level=risk,
                    risks=[f"{a.get('destroy')} resource(s) destroyed" if a.get("destroy") else "in-place/added resources only",
                           f"sensitive resources touched: {a.get('sensitive_resources')}" if a.get("sensitive_resources") else "no sensitive resource types in plan"],
                    rollback=["restore the previous configuration from git", "terraform plan (verify it reverts the change)", "terraform apply"],
                    validation=["terraform plan shows no further changes after apply", "resource health checks (kubectl get nodes / aws describe)"],
                    required_permissions=["terraform.apply"], steps=["review plan", "approve", "terraform apply", "verify"])
        plan.changes.append(ProposedChange(description=f"terraform apply ({a.get('add')} add / {a.get('change')} change / {a.get('destroy')} destroy)", kind="infrastructure",
                                           target=d or ".", tool="terraform_apply", args={"dir": d, "allow_destroy": False}, risk=risk, permission=PermissionLevel.DEPLOY,
                                           rollback="restore previous configuration and re-apply", environment=inv.task.environment.value))
        if a.get("change") or a.get("add"):
            plan.cost_notes.append("resource additions/changes may alter cloud spend; no pricing data configured so no estimate is given")
        return plan

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        if not any(c.applied and c.tool == "terraform_apply" for c in plan.changes):
            return []
        res = self.call(inv, "terraform_plan", {"dir": inv.target("terraform_dir")}, purpose="verify no drift remains after apply")
        no_changes = res.ok and res.output.get("analysis", {}).get("no_changes")
        return [ValidationResult("terraform plan after apply", bool(res.ok), "no changes pending" if no_changes else "plan still shows changes (mock backend returns the same plan)" if res.ok else res.error or "")]


def _invalid(log: EvidenceLog) -> list[Hypothesis]:
    if log.has("tf_valid", False):
        return [Hypothesis(statement=f"Terraform configuration is invalid: {(log.get('tf_validate_error') or '')[:150]}", validation="terraform validate output.", status="confirmed", confidence=0.95)]
    if log.get("tf_plan_failed"):
        return [Hypothesis(statement=f"terraform plan fails: {(log.get('tf_plan_error') or '')[:150]}", validation="plan stderr.", status="confirmed", confidence=0.9)]
    return []


def _plan_risk(log: EvidenceLog) -> list[Hypothesis]:
    a = log.get("tf_plan")
    if not a:
        return []
    if a.get("no_changes"):
        return [Hypothesis(statement="Terraform state matches configuration (no changes).", validation="plan output", status="confirmed", confidence=0.9)]
    log.recommendation(f"Review the {a.get('risk')}-risk plan before apply; destroys={a.get('destroy')}, replacements={a.get('replaced')}.")
    return [Hypothesis(statement=f"Plan changes infrastructure: +{a.get('add')} ~{a.get('change')} -{a.get('destroy')} (risk {a.get('risk')}).", validation="plan output",
                       status="confirmed", confidence=0.9)]
