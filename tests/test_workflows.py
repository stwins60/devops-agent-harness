"""End-to-end definition-of-done workflows in mock mode."""
from pathlib import Path

import yaml

from agent.models import TaskKind, TaskStage, TaskStatus


def test_question_gives_evidence_backed_diagnosis(make_harness):
    h = make_harness()
    t = h.run("Why is my Kubernetes API deployment failing?")
    assert t.status == TaskStatus.COMPLETED and t.mode.value == "read-only"
    assert "Probe port mismatch" in t.diagnosis.conclusion
    assert t.report and "## Root Cause" in t.report and "## Evidence" in t.report
    assert h.store.read_artifact(t.id, "evidence.md") and h.store.read_artifact(t.id, "final-report.md")
    assert h.world.mutations == []


def test_jira_ticket_end_to_end(make_harness):
    h = make_harness()
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.COMPLETED and t.stage == TaskStage.DONE
    # read Jira, understand requirements, inspect repository, diagnose
    assert t.links.jira_issue == "DEVOPS-382"
    assert any("Acceptance criteria" in f.statement for f in t.facts())
    assert "Probe port mismatch" in t.diagnosis.conclusion
    # plan + approved fix implemented in the workspace copy of the repo
    assert t.plan.approved and t.plan.changes and all(c.applied for c in t.plan.changes)
    manifest = yaml.safe_load((Path(t.workspace) / "k8s" / "deployment.yaml").read_text())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["port"] == 8080 and container["livenessProbe"]["httpGet"]["port"] == 8080
    # tests + security checks ran and passed
    names = {v.name: v for v in t.validation}
    assert names["manifest consistency (probe ports)"].passed and names["project tests (pytest)"].passed and names["secret scan (built-in)"].passed
    # branch, commit, push, PR, Jira update
    assert t.links.branch.startswith("fix/DEVOPS-382-") and t.links.commit and t.links.pull_request.endswith("/pull/421")
    kinds = [m["kind"] for m in h.world.mutations]
    assert kinds.index("git_branch") < kinds.index("git_commit") < kinds.index("git_push") < kinds.index("github_create_pr") < kinds.index("jira_comment")
    issue = h.world.jira["issues"]["DEVOPS-382"]
    assert issue["status"] == "In Review" and "devops-agent" in issue["labels"] and "pull/421" in issue["comments"][-1]["body"]
    # final report with traceability chain
    report = h.store.read_artifact("DEVOPS-382", "final-report.md")
    assert "Jira: DEVOPS-382" in report and "Pull Request: https://github.com/example-org/sample-app/pull/421" in report
    for name in ("plan.md", "evidence.md", "changes.md", "validation.md"):
        assert h.store.read_artifact("DEVOPS-382", name)
    # the source example repository was never modified
    src = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples" / "sample-app" / "k8s" / "deployment.yaml").read_text())
    assert src["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["port"] == 8000


def test_jira_dry_run_changes_nothing(make_harness):
    h = make_harness(dry_run=True)
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.COMPLETED and t.dry_run
    assert t.plan and t.plan.changes and not any(c.applied for c in t.plan.changes)
    assert t.plan.changes[0].diff and "8080" in t.plan.changes[0].diff
    manifest = yaml.safe_load((Path(t.workspace) / "k8s" / "deployment.yaml").read_text())
    assert manifest["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["port"] == 8000
    assert h.world.mutations == [] and h.world.jira["issues"]["DEVOPS-382"]["status"] == "To Do"


def test_incident_response_workflow(make_harness):
    h = make_harness()
    t = h.run("production API is returning 503", kind=TaskKind.INCIDENT)
    assert t.status == TaskStatus.COMPLETED
    assert any("severity SEV1" in f.statement for f in t.facts())
    assert any("5xx ratio 98.0%" in f.statement for f in t.facts())
    assert "Probe port mismatch" in t.diagnosis.conclusion
    assert t.plan.changes[0].tool == "kubectl_rollout_undo" and t.plan.changes[0].applied
    assert [m["kind"] for m in h.world.mutations] == ["kubectl_rollout_undo"]
    assert all(v.passed for v in t.validation)
    report = h.store.read_artifact(t.id, "incident-report.md")
    for section in ("## Timeline", "## Impact", "## Root Cause", "## Contributing Factors", "## Mitigation", "## Corrective Actions", "## Preventative Actions"):
        assert section in report
    assert "SEV1" in report and "[applied]" in report
    assert (h.config.agent_dir / "incidents").exists() and list((h.config.agent_dir / "incidents").glob("*.md"))


def test_incident_without_explicit_approval_pauses(make_harness):
    h = make_harness(allow_explicit=False)
    t = h.run("production API is returning 503", kind=TaskKind.INCIDENT)
    assert t.status == TaskStatus.PAUSED and not t.plan.approved
    assert h.world.mutations == [] and "approve-all" in t.notes[-1]


def test_change_plan_workflow(make_harness):
    h = make_harness()
    t = h.run("upgrade our Kubernetes worker nodes", kind=TaskKind.PLAN)
    assert t.status == TaskStatus.COMPLETED
    plan_md = h.store.read_artifact(t.id, "plan.md")
    for section in ("## Problem", "## Root Cause", "## Evidence", "## Proposed Changes", "## Files", "## Infrastructure", "## Risks", "## Rollback", "## Validation", "## Required Permissions"):
        assert section in plan_md
    assert "terraform apply" in plan_md.lower() and "node group" in plan_md.lower()
    assert h.world.mutations == []


def test_execute_only_runs_policy_permitted_actions(make_harness):
    # autonomous mode + production: file edits are auto-allowed (workspace), the push/PR need approval (auto-approved), no infra mutation happens
    h = make_harness(mode="autonomous")
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.COMPLETED
    infra = [m for m in h.world.mutations if m["kind"].startswith("kubectl")]
    assert infra == []
    approvals = [a for a in t.approvals if a.decision == "approve"]
    assert approvals and any(a.request["operation"].startswith("git_push") for a in t.approvals)
