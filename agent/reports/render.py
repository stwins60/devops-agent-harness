"""Markdown renderers for plans, evidence, changes, validation, final and incident reports."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from agent.models import Diagnosis, Plan, ValidationResult
from agent.rca.engine import RootCauseEngine

if TYPE_CHECKING:  # pragma: no cover
    from agent.state.store import TaskState


def _list(items: list[str], empty: str = "None") -> str:
    return "\n".join(f"- {i}" for i in items) if items else f"_{empty}_"


def _numbered(items: list[str], empty: str = "None") -> str:
    return "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1)) if items else f"_{empty}_"


def render_plan(plan: Plan) -> str:
    changes = []
    for n, c in enumerate(plan.changes, 1):
        line = f"{n}. **{c.description}**  \n   kind: {c.kind}, target: `{c.target}`, risk: {c.risk.value}, permission: {c.permission.name}"
        if c.rollback:
            line += f"  \n   rollback: {c.rollback}"
        if c.diff:
            line += f"\n\n```diff\n{c.diff.strip()}\n```"
        changes.append(line)
    return "\n".join([
        f"# Task\n\n{plan.task_id} - {plan.title}", "",
        f"## Problem\n\n{plan.problem or '_not stated_'}", "",
        f"## Root Cause\n\n{plan.root_cause or '_not yet confirmed_'}", "",
        f"## Evidence\n\n{_list(plan.evidence, 'no evidence collected')}", "",
        f"## Proposed Changes\n\n{chr(10).join(changes) if changes else '_no changes proposed_'}", "",
        f"## Files\n\n{_list(plan.files, 'no files affected')}", "",
        f"## Infrastructure\n\n{_list(plan.infrastructure, 'no infrastructure affected')}", "",
        f"## Risks\n\nOverall risk: **{plan.risk_level.value.upper()}**\n\n{_list(plan.risks, 'no specific risks identified')}", "",
        f"## Rollback\n\n{_numbered(plan.rollback, 'no rollback needed (no mutations)')}", "",
        f"## Validation\n\n{_list(plan.validation, 'no validation steps defined')}", "",
        f"## Required Permissions\n\n{_list(plan.required_permissions, 'read-only')}", "",
        (f"## Cost\n\n{_list(plan.cost_notes)}\n" if plan.cost_notes else ""),
        f"## Steps\n\n{_numbered(plan.steps, 'see proposed changes')}", "",
        f"## Approval\n\n{'Approved' if plan.approved else 'Not yet approved'}{(' - ' + plan.approval_note) if plan.approval_note else ''}",
    ])


def render_evidence(state: "TaskState") -> str:
    lines = [f"# Evidence - {state.id}", "", f"Request: {state.request}", ""]
    for e in state.evidence:
        src = f"  \n  _source: {e.source}_" if e.source else ""
        lines.append(f"**{e.kind.value}**: {e.statement}{src}\n")
    if state.diagnosis:
        lines += ["", "## Root Cause Analysis", "", "```text", RootCauseEngine.render(state.diagnosis), "```"]
    return "\n".join(lines)


def render_changes(state: "TaskState") -> str:
    lines = [f"# Changes - {state.id}", ""]
    if not state.changes:
        lines.append("_No changes were made._")
    for c in state.changes:
        status = "APPLIED" if c.applied else ("DRY-RUN" if state.dry_run else "PROPOSED")
        lines.append(f"## [{status}] {c.description}")
        lines.append(f"- kind: {c.kind}\n- target: `{c.target}`\n- risk: {c.risk.value}\n- permission: {c.permission.name}")
        if c.rollback:
            lines.append(f"- rollback: {c.rollback}")
        if c.diff:
            lines += ["", "```diff", c.diff.strip(), "```"]
        lines.append("")
    mutations = [t for t in state.tool_calls if t.rollback or t.tool.startswith(("git_", "kubectl_apply", "terraform_apply"))]
    if mutations:
        lines += ["## Mutating tool calls", ""]
        for t in mutations:
            lines.append(f"- {t.timestamp} `{t.tool}` {'OK' if t.ok else 'FAILED'}{' (dry-run)' if t.dry_run else ''} - {t.summary[:160]}")
    return "\n".join(lines)


def render_validation(results: list[ValidationResult], task_id: str = "") -> str:
    lines = [f"# Validation - {task_id}", ""]
    if not results:
        lines.append("_No validation was run._")
    for r in results:
        lines.append(f"- {r.name}: **{r.label}**" + (f" - {r.detail}" if r.detail else ""))
    passed = all(r.passed or r.skipped for r in results) if results else False
    lines += ["", f"Overall: **{'PASS' if passed else 'FAIL'}**" if results else "Overall: **NOT VALIDATED**"]
    return "\n".join(lines)


def render_diagnosis_summary(diag: Optional[Diagnosis]) -> str:
    if not diag:
        return "_No root cause analysis was performed._"
    return "```text\n" + RootCauseEngine.render(diag) + "\n```"


def render_final_report(state: "TaskState", *, security: Optional[list[str]] = None, deployment: Optional[str] = None,
                        remaining_risks: Optional[list[str]] = None, follow_up: Optional[list[str]] = None,
                        rollback_text: Optional[str] = None, metrics: Optional[dict[str, Any]] = None) -> str:
    plan = state.plan
    facts = [f"{e.statement} _(source: {e.source})_" if e.source else e.statement for e in state.facts()]
    files = sorted({c.target for c in state.changes if c.kind == "file"})
    infra = sorted({c.target for c in state.changes if c.kind in ("infrastructure", "command")})
    applied = [c for c in state.changes if c.applied]
    change_lines = []
    for c in state.changes:
        tag = "applied" if c.applied else ("dry-run, not applied" if state.dry_run else "proposed, NOT applied")
        change_lines.append(f"{c.description} [{tag}]")
    validation = "\n".join(f"- {r.name}: {r.label}" + (f" - {r.detail}" if r.detail else "") for r in state.validation) or "_No validation was run._"
    status_line = {
        "completed": "Completed", "failed": "FAILED", "denied": "Stopped: approval denied", "blocked": "BLOCKED - human action required",
        "waiting_approval": "Waiting for approval", "paused": "Paused (resumable)", "running": "In progress", "pending": "Pending",
    }.get(state.status.value, state.status.value)
    links = state.links
    m = metrics or state.metrics or {}
    counters = m.get("counters", {}) if isinstance(m, dict) else {}
    parts = [
        "# DevOps Task Report", "",
        f"## Task\n\n{state.id}  \nKind: {state.kind.value}  \nMode: {state.mode.value}  \nEnvironment: {state.environment.value}  \nStatus: **{status_line}**",
        "", f"## Objective\n\n{state.request}", "",
        "## Investigation\n\n" + (_list(state.specialists and [f"Specialist: {s}" for s in state.specialists]) + "\n\n" if state.specialists else "") +
        (f"Tool calls: {len(state.tool_calls)}" if state.tool_calls else "_No tools were executed._"), "",
        f"## Root Cause\n\n{(state.diagnosis.conclusion if state.diagnosis and state.diagnosis.conclusion else 'Not confirmed. ' + (('Leading hypothesis: ' + max(state.diagnosis.hypotheses, key=lambda h: h.confidence).statement) if state.diagnosis and state.diagnosis.hypotheses else 'No hypothesis could be validated with the available evidence.'))}", "",
        f"## Evidence\n\n{_list(facts, 'no evidence collected')}", "",
        f"## Changes\n\n{_list(change_lines, 'no changes were made')}", "",
        f"## Files Changed\n\n{_list([f for f in files if any(c.applied for c in state.changes if c.target == f)] , 'none')}", "",
        f"## Infrastructure Changes\n\n{_list([i for i in infra if any(c.applied for c in state.changes if c.target == i)], 'none')}", "",
        f"## Validation\n\n{validation}", "",
        f"## Security Checks\n\n{_list(security or [], 'no security checks were run')}", "",
        f"## Deployment\n\n{deployment or 'No deployment was performed by this task.'}", "",
        f"## Rollback\n\n{rollback_text or (chr(10).join(f'- {r}' for r in plan.rollback) if plan and plan.rollback else 'No mutating operations were performed; nothing to roll back.')}", "",
        f"## Jira\n\n{links.jira_issue or '_not linked_'}", "",
        f"## Git\n\n" + _list([x for x in (f'Repository: {links.repository}' if links.repository else None, f'Branch: {links.branch}' if links.branch else None, f'Commit: {links.commit}' if links.commit else None) if x], 'no git activity'), "",
        f"## Pull Request\n\n{links.pull_request or '_none created_'}", "",
        f"## Traceability\n\n{_list(links.chain(), 'no links')}", "",
        f"## Remaining Risks\n\n{_list(remaining_risks or (plan.risks if plan else []), 'none identified')}", "",
        f"## Recommended Follow-Up\n\n{_list(follow_up or (state.diagnosis.recommendations if state.diagnosis else []), 'none')}", "",
        f"## Harness Metrics\n\n- tool calls: {counters.get('tool.calls', len(state.tool_calls))}\n- tool failures: {counters.get('tool.failures', 0)}\n- approvals requested: {counters.get('approval.requested', len(state.approvals))}\n- policy blocks: {counters.get('policy.blocked', 0)}\n- model calls: {counters.get('model.calls', 0)}",
    ]
    if state.errors:
        parts += ["", "## Errors\n\n" + _list(state.errors)]
    if not applied and state.changes and not state.dry_run:
        parts += ["", "> NOTE: changes above were proposed but NOT applied. Nothing was modified."]
    return "\n".join(parts)


def render_incident_report(state: "TaskState", *, severity: str, timeline: list[str], impact: str, contributing: list[str],
                           mitigation: list[str], corrective: list[str], preventive: list[str]) -> str:
    diag = state.diagnosis
    root = diag.conclusion if diag and diag.conclusion else "Not confirmed - see hypotheses in evidence.md"
    return "\n".join([
        f"# Incident Report - {state.id}", "",
        f"**Severity:** {severity}  \n**Status:** {state.status.value}  \n**Environment:** {state.environment.value}", "",
        f"## Summary\n\n{state.request}", "",
        f"## Timeline\n\n{_list(timeline, 'no timeline entries')}", "",
        f"## Impact\n\n{impact}", "",
        f"## Root Cause\n\n{root}", "",
        f"## Evidence\n\n{_list([e.statement for e in state.facts()], 'none')}", "",
        f"## Contributing Factors\n\n{_list(contributing, 'none identified')}", "",
        f"## Mitigation\n\n{_list(mitigation, 'no mitigation applied')}", "",
        f"## Corrective Actions\n\n{_list(corrective, 'none')}", "",
        f"## Preventative Actions\n\n{_list(preventive, 'none')}", "",
        f"## Traceability\n\n{_list(state.links.chain(), 'no links')}",
    ])
