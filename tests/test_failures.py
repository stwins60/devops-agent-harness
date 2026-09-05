"""Failure-recovery scenarios: the harness must stop cleanly, say what happened and never fake success."""
from agent.models import TaskKind, TaskStatus


def test_jira_unavailable_blocks_with_explicit_reason(make_harness):
    h = make_harness(flags={"jira_unavailable": True})
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.BLOCKED and "Jira unavailable" in t.notes[-1]
    assert t.plan is None and h.world.mutations == []
    assert t.report and "BLOCKED" in t.report


def test_kubernetes_unreachable_blocks(make_harness):
    t = make_harness(flags={"k8s_unreachable": True}).run("why is my kubernetes deployment api failing?")
    assert t.status == TaskStatus.BLOCKED and "cannot" in t.notes[-1]
    assert any(f.data.get("tool_failure") == "network" for f in t.facts())


def test_permission_denied_is_not_retried(make_harness):
    h = make_harness(flags={"permission_denied": True})
    t = h.run("why is my kubernetes deployment api failing?")
    assert t.status == TaskStatus.BLOCKED
    calls = [c for c in t.tool_calls if c.tool == "kubectl_get"]
    assert len(calls) == 1  # no blind retry on Forbidden


def test_aws_credentials_expired_reported(make_harness):
    t = make_harness(flags={"aws_creds_expired": True}).run("check the eks nodegroup versions in aws")
    assert t.status == TaskStatus.BLOCKED and "AWS access unavailable" in t.notes[-1]
    assert any("ExpiredToken" in f.statement for f in t.facts())


def test_git_push_rejected_keeps_changes_and_reports(make_harness):
    h = make_harness(flags={"git_push_rejected": True})
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.COMPLETED
    assert t.links.pull_request is None and any("push failed" in e for e in t.errors)
    assert h.world.jira["issues"]["DEVOPS-382"]["status"] == "In Progress"  # not "In Review": no PR exists
    assert "changes-applied-not-delivered" in h.world.jira["issues"]["DEVOPS-382"]["comments"][-1]["body"]


def test_pr_creation_failure(make_harness):
    h = make_harness(flags={"pr_create_fails": True})
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.COMPLETED and t.links.pull_request is None
    assert any("PR creation failed" in e for e in t.errors)
    assert [m["kind"] for m in h.world.mutations if m["kind"] == "git_push"]  # branch was pushed, so a human can open the PR


def test_partial_deployment_triggers_rollback(make_harness):
    h = make_harness(flags={"partial_deploy": True})
    t = h.run("production API is returning 503", kind=TaskKind.INCIDENT)
    assert t.status == TaskStatus.FAILED and "validation failed" in t.notes[-1]
    kinds = [m["kind"] for m in h.world.mutations]
    assert kinds.count("kubectl_rollout_undo") >= 2  # mitigation + automatic rollback of the mitigation
    assert "rolled back" in t.notes[-2] or any("rolled back" in n for n in t.notes)


def test_rollback_failure_is_reported_explicitly(make_harness):
    h = make_harness(flags={"rollback_fails": True})
    # make validation fail by breaking the manifest test after the fix: remove the container port so the consistency check fails
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382", dry_run=False)
    # normal run succeeds; simulate a rollback attempt on the applied change and assert the failure is explicit
    entries = h.executor.rollback_all(t)
    assert entries and entries[0].ok is False
    assert "ROLLBACK FAILED" in h.executor.rollback_plan(t).render()


def test_tool_timeout_is_surfaced(make_harness):
    t = make_harness(scenario="disk-full", flags={"tool_timeout": True}).run("the api service on host api-host-01 is failing")
    assert t.status == TaskStatus.BLOCKED and "unreachable" in t.notes[-1]
    assert any(f.data.get("tool_failure") == "timeout" for f in t.facts())


def test_denied_plan_never_mutates(make_harness):
    from agent.approvals.engine import RecordingHandler
    from agent.models import ApprovalDecision

    h = make_harness(handler=RecordingHandler(default=ApprovalDecision.DENY))
    t = h.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.DENIED and h.world.mutations == []
    assert not any(c.applied for c in t.plan.changes)
    assert "NOT applied" in t.report


def test_unexpected_exception_persists_failed_state(make_harness, monkeypatch):
    h = make_harness()
    sp = h.specialists["kubernetes-agent"]

    def boom(inv):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sp, "investigate", boom)
    try:
        h.run("why is my kubernetes deployment failing?")
    except RuntimeError:
        pass
    tasks = h.store.list()
    assert tasks and tasks[0].status == TaskStatus.FAILED and any("kaboom" in e for e in tasks[0].errors)
