from agent.approvals.engine import RecordingHandler
from agent.models import ApprovalDecision, Environment, Evidence, EvidenceKind, OperatingMode, Plan, ProposedChange, TaskKind, TaskStage, TaskStatus
from agent.state.store import TaskStore


def test_task_store_roundtrip(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    t = store.create("Fix DEVOPS-1", kind=TaskKind.JIRA, mode=OperatingMode.APPROVAL, environment=Environment.DEV)
    assert t.id == "DEVOPS-1"
    t.add_evidence(Evidence(EvidenceKind.FACT, "pod crashed", source="kubectl", data={"pod": "x"}))
    t.plan = Plan(task_id=t.id, title="fix", changes=[ProposedChange(description="c", kind="file", target="a.yaml", tool="fs_replace", args={"path": "a.yaml", "old": "1", "new": "2"})])
    t.transition(TaskStage.VALIDATION, TaskStatus.RUNNING, "testing")
    t.links.branch = "fix/DEVOPS-1-x"
    store.save(t)
    loaded = store.load("DEVOPS-1")
    assert loaded.stage == TaskStage.VALIDATION and loaded.evidence[0].data["pod"] == "x" and loaded.plan.changes[0].tool == "fs_replace"
    assert loaded.links.branch == "fix/DEVOPS-1-x" and loaded.history[-1]["to"] == "validation"
    assert store.list()[0].id == "DEVOPS-1"
    assert store.resumable("DEVOPS-1") == (True, "resume from stage 'validation' (status running)")


def test_create_archives_completed_task_with_same_id(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    t = store.create("Fix DEVOPS-2", task_id="DEVOPS-2")
    t.status = TaskStatus.COMPLETED
    store.save(t)
    t2 = store.create("Fix DEVOPS-2", task_id="DEVOPS-2")
    assert t2.status == TaskStatus.PENDING and (tmp_path / "tasks" / "_archive").exists()


def test_invalid_task_id_rejected(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    try:
        store.dir("../escape")
    except ValueError:
        return
    raise AssertionError("path traversal id accepted")


def test_pause_on_missing_approval_then_resume(make_harness):
    # first run: nobody can approve -> task pauses at the approval gate, nothing mutated
    h1 = make_harness(auto_approve=False, handler=None)
    t = h1.run("Fix DEVOPS-382", kind=TaskKind.JIRA, task_id="DEVOPS-382")
    assert t.status == TaskStatus.PAUSED and t.stage == TaskStage.APPROVAL
    assert t.plan and not t.plan.approved and not any(c.applied for c in t.plan.changes)
    assert "resume with" in t.notes[-1]
    assert h1.world.mutations == []

    # second run (new process, new world): resume with an approver present
    h2 = make_harness(handler=RecordingHandler(default=ApprovalDecision.APPROVE))
    r = h2.resume("DEVOPS-382")
    assert r.status == TaskStatus.COMPLETED and r.stage == TaskStage.DONE
    assert r.plan.approved and all(c.applied for c in r.plan.changes)
    assert r.links.pull_request and "/pull/421" in r.links.pull_request
    assert h2.world.jira["issues"]["DEVOPS-382"]["status"] == "In Review"
    assert h2.store.read_artifact("DEVOPS-382", "final-report.md")


def test_resume_refuses_completed_task(make_harness):
    h = make_harness()
    t = h.run("Why is my Kubernetes API deployment failing?")
    assert t.status == TaskStatus.COMPLETED
    again = h.resume(t.id)
    assert again.status == TaskStatus.COMPLETED and again.id == t.id
