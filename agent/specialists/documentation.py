"""Documentation specialist: writes task artifacts, incident reports and project memory."""
from __future__ import annotations

from typing import Any, Optional

from agent.audit.redaction import redact_text
from agent.memory.store import MemoryError
from agent.models import Diagnosis, Plan
from agent.reports.render import render_changes, render_evidence, render_final_report, render_incident_report, render_plan, render_validation
from agent.specialists.base import Investigation, Specialist


class DocumentationSpecialist(Specialist):
    name = "documentation-agent"
    description = "Produces plan/evidence/changes/validation/final reports, incident postmortems and durable project memory."
    domains = ["documentation"]
    keywords = ["document", "documentation", "report", "postmortem", "runbook", "readme"]

    def investigate(self, inv: Investigation) -> None:
        return None

    def write_artifacts(self, inv: Investigation, *, security: Optional[list[str]] = None, deployment: Optional[str] = None,
                        rollback_text: Optional[str] = None, incident: Optional[dict[str, Any]] = None) -> str:
        task = inv.task
        store = self.h.store
        store.write_artifact(task.id, "evidence.md", render_evidence(task))
        if task.plan:
            store.write_artifact(task.id, "plan.md", render_plan(task.plan))
        store.write_artifact(task.id, "changes.md", render_changes(task))
        store.write_artifact(task.id, "validation.md", render_validation(task.validation, task.id))
        metrics = self.h.audit.metrics.snapshot()
        task.metrics = metrics
        report = render_final_report(task, security=security, deployment=deployment, rollback_text=rollback_text, metrics=metrics)
        if incident:
            inc = render_incident_report(task, **incident)
            store.write_artifact(task.id, "incident-report.md", inc)
            try:
                self.h.memory.remember("incidents", f"{task.id} {task.request[:60]}", inc, tags=["incident", task.environment.value])
            except MemoryError as exc:
                task.note(f"memory write skipped: {exc}")
        store.write_artifact(task.id, "final-report.md", report)
        task.report = report
        return report

    def remember_outcome(self, inv: Investigation, diagnosis: Optional[Diagnosis], plan: Optional[Plan]) -> None:
        if not diagnosis or not diagnosis.conclusion:
            return
        body = [f"Request: {inv.task.request}", f"Root cause: {diagnosis.conclusion}", ""]
        body += [f"- {f.statement}" for f in diagnosis.facts[:6]]
        if plan and plan.changes:
            body += ["", "Fix:"] + [f"- {c.description}" for c in plan.changes[:4]]
        try:
            self.h.memory.remember("memory", f"failure mode: {diagnosis.conclusion[:70]}", redact_text("\n".join(body)), tags=[inv.task.kind.value, *inv.task.specialists])
        except MemoryError as exc:
            inv.task.note(f"memory write skipped: {exc}")
