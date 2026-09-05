"""CI/CD specialist: pipeline -> job -> step -> log analysis -> root cause -> fix -> validation."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class CiCdSpecialist(Specialist):
    name = "cicd-agent"
    description = "Investigates GitHub Actions / GitLab CI failures and proposes fixes."
    domains = ["cicd"]
    keywords = ["pipeline", "ci", "cd", "workflow", "github actions", "gitlab ci", "jenkins", "argocd", "flux", "build", "job", "runner", "actions"]

    def investigate(self, inv: Investigation) -> None:
        self.use_runbook(inv, inv.task.request, domain="cicd")
        runs = self.call(inv, "cicd_list_runs", {"branch": inv.target("branch"), "limit": 10}, purpose="recent pipeline runs")
        if not runs.ok:
            if runs.failure_kind in ("auth", "network", "permission", "unavailable"):
                inv.blocked = f"CI provider unavailable: {runs.error}"
            return
        items = runs.output.get("runs", [])
        target = None
        if inv.target("run_id"):
            target = next((r for r in items if r.get("id") == inv.target("run_id")), None)
        if target is None:
            target = next((r for r in items if r.get("conclusion") in ("failure", "failed")), None)
        if target is None:
            inv.log.fact(f"No failed pipeline runs among the last {len(items)}; latest is {items[0].get('conclusion') if items else 'n/a'}.", source="cicd_list_runs", ci_all_green=True)
            return
        inv.log.fact(f"Failed run {target.get('id')} on branch {target.get('branch')} (sha {target.get('sha')}): {target.get('url')}", source="cicd_list_runs",
                     run_id=target.get("id"), run_branch=target.get("branch"))
        inv.task.links.pipeline = target.get("url") or str(target.get("id"))
        inv.task.links.commit = target.get("sha")
        jobs = self.call(inv, "cicd_run_jobs", {"run_id": target.get("id")}, purpose="jobs of the failed run")
        if not jobs.ok:
            return
        failed = [j for j in jobs.output.get("jobs", []) if j.get("conclusion") in ("failure", "failed")]
        for j in failed[:2]:
            inv.log.fact(f"Job '{j.get('name')}' failed at step(s) {j.get('failed_steps')}.", source="cicd_run_jobs", failed_job=j.get("name"), failed_steps=j.get("failed_steps"))
            logs = self.call(inv, "cicd_job_logs", {"job_id": j.get("id")}, purpose="failed job log")
            if logs.ok:
                analysis = logs.output.get("analysis", {})
                findings = analysis.get("findings", [])
                inv.log.fact(f"Log analysis of job '{j.get('name')}': " + ("; ".join(f"{f['label']}: {f['match']}" for f in findings[:4]) or "no known error signature") + ".",
                             source=f"cicd_job_logs({j.get('id')})", log_findings=findings, error_lines=analysis.get("error_lines", [])[:5])

    def analyzers(self):
        return [("cicd.log_signature", _log_signature), ("cicd.green", _green)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        findings = inv.log.get("log_findings") or []
        if not findings:
            return None
        plan = Plan(task_id=inv.task.id, title=f"Fix CI failure in job '{inv.log.get('failed_job')}'", problem=diagnosis.problem, root_cause=diagnosis.conclusion or "",
                    evidence=[f.statement for f in diagnosis.facts][:10], validation=["re-run the failed job and confirm success", "lint/tests pass locally"],
                    rollback=["revert the fix commit"], risk_level=RiskLevel.LOW, infrastructure=[f"pipeline {inv.task.links.pipeline}"])
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        for f in findings:
            if f["label"] == "lint: unused import" and repo:
                m = re.search(r"F401 '([^']+)'", f["match"])
                loc = re.search(r"([\w/.\-]+\.py):(\d+)", " ".join(inv.log.get("error_lines") or []) + f["match"])
                if m and loc:
                    path, line_no = loc.group(1), int(loc.group(2))
                    target = repo / path
                    if target.exists():
                        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
                        if 0 < line_no <= len(lines) and m.group(1).split(".")[0] in lines[line_no - 1]:
                            plan.changes.append(self.file_change(f"Remove unused import '{m.group(1)}' from {path}", path, lines[line_no - 1], ""))
                            plan.files.append(path)
                            continue
            plan.changes.append(ProposedChange(description=f"{f['label']}: {f['hint']} ({f['match'][:80]})", kind="file", target=inv.log.get("failed_job") or "pipeline", tool=None,
                                               risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY, rollback="git revert"))
        plan.steps = ["apply the fixes on a branch", "run lint/tests locally", "push and let CI validate", "re-run the failed workflow if needed"]
        return plan

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        return []


def _log_signature(log: EvidenceLog) -> list[Hypothesis]:
    findings = log.get("log_findings") or []
    if not findings:
        if log.has("failed_job"):
            return [Hypothesis(statement=f"Job '{log.get('failed_job')}' failed without a recognised error signature.", validation="Read the full job log manually.",
                               status="unvalidated", confidence=0.3)]
        return []
    primary = next((f for f in findings if f["label"] not in ("non-zero exit", "actions error annotation")), findings[0])
    log.recommendation(f"{primary['hint']} (from: {primary['match'][:100]})")
    return [Hypothesis(statement=f"Job '{log.get('failed_job')}' failed because of {primary['label']}: {primary['match'][:120]}",
                       validation="Error signature present in the failed step's log.", status="confirmed", confidence=0.9)]


def _green(log: EvidenceLog) -> list[Hypothesis]:
    if log.get("ci_all_green"):
        return [Hypothesis(statement="No failing pipeline runs found.", validation="run list", status="confirmed", confidence=0.8)]
    return []
