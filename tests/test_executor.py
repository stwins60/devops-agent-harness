from agent.approvals.engine import RecordingHandler
from agent.models import ApprovalDecision, Environment, OperatingMode
from agent.state.store import TaskState


def task(h, mode=OperatingMode.AUTONOMOUS, env=Environment.LOCAL, dry_run=False, workspace=None):
    t = TaskState(id="T-EXEC", request="exec test", mode=mode, environment=env, dry_run=dry_run, workspace=str(workspace) if workspace else None)
    h.store.save(t)
    return t


def test_unknown_tool_and_invalid_args(make_harness):
    h = make_harness()
    t = task(h)
    r = h.executor.run("nope", {}, t)
    assert not r.ok and r.failure_kind == "invalid"
    r2 = h.executor.run("kubectl_logs", {}, t)
    assert not r2.ok and "missing required" in r2.error


def test_policy_block_is_audited_and_recorded(make_harness):
    h = make_harness()
    t = task(h, mode=OperatingMode.READ_ONLY)
    r = h.executor.run("kubectl_rollout_restart", {"name": "api", "namespace": "production"}, t)
    assert not r.ok and r.failure_kind == "policy"
    assert any(rec["event"] == "policy_block" for rec in h.audit.records)
    assert t.tool_calls[-1].tool == "kubectl_rollout_restart" and not t.tool_calls[-1].ok


def test_approval_denied_and_skipped(make_harness):
    handler = RecordingHandler([ApprovalDecision.DENY, ApprovalDecision.SKIP])
    h = make_harness(handler=handler)
    t = task(h, mode=OperatingMode.APPROVAL, env=Environment.PRODUCTION)
    r = h.executor.run("kubectl_rollout_undo", {"name": "api", "namespace": "production"}, t)
    assert not r.ok and r.failure_kind == "denied"
    r2 = h.executor.run("kubectl_rollout_undo", {"name": "api", "namespace": "production"}, t)
    assert r2.skipped and r2.failure_kind == "skipped"
    assert len(t.approvals) == 2 and handler.requests[0].environment == "production" and "rollout undo" in handler.requests[0].rollback
    assert h.world.mutations == []


def test_loop_guard_stops_repeated_identical_calls(make_harness):
    h = make_harness()
    h.config.limits.max_repeated_calls = 2
    t = task(h)
    for _ in range(2):
        assert h.executor.run("kubectl_get", {"kind": "pods", "namespace": "production"}, t).ok
    r = h.executor.run("kubectl_get", {"kind": "pods", "namespace": "production"}, t)
    assert not r.ok and r.failure_kind == "loop_guard"


def test_tool_call_budget(make_harness):
    h = make_harness()
    h.config.limits.max_tool_calls = 1
    t = task(h)
    assert h.executor.run("kubectl_current_context", {}, t).ok
    assert h.executor.run("kubectl_get_nodes", {}, t).failure_kind == "loop_guard"


def test_dry_run_never_mutates(make_harness, tmp_path):
    h = make_harness()
    t = task(h, dry_run=True, workspace=tmp_path)
    r = h.executor.run("fs_write", {"path": "new.txt", "content": "hello"}, t)
    assert r.ok and r.dry_run and not (tmp_path / "new.txt").exists()
    r2 = h.executor.run("kubectl_apply", {"manifest": "kind: Deployment\nmetadata: {name: api, namespace: production}\nspec: {replicas: 1, template: {spec: {containers: [{name: api}]}}}"}, t)
    assert r2.ok and r2.dry_run and h.world.mutations == []
    assert t.checkpoint.get("rollback", []) == []


def test_rollback_all_restores_files_in_reverse_order(make_harness, tmp_path):
    h = make_harness()
    (tmp_path / "a.txt").write_text("orig")
    t = task(h, workspace=tmp_path)
    assert h.executor.run("fs_write", {"path": "a.txt", "content": "changed"}, t).ok
    assert h.executor.run("fs_write", {"path": "b.txt", "content": "new"}, t).ok
    plan = h.executor.rollback_plan(t)
    assert [e.tool for e in plan.entries] == ["fs_write", "fs_write"]
    entries = h.executor.rollback_all(t)
    assert all(e.ok for e in entries)
    assert (tmp_path / "a.txt").read_text() == "orig" and not (tmp_path / "b.txt").exists()
    assert any(rec["event"] == "rollback" for rec in h.audit.records)


def test_rollback_failure_is_explicit(make_harness, tmp_path):
    h = make_harness(flags={"rollback_fails": True})
    t = task(h, workspace=tmp_path)
    assert h.executor.run("fs_write", {"path": "a.txt", "content": "x"}, t).ok
    entries = h.executor.rollback_all(t)
    assert entries and entries[0].ok is False and "ROLLBACK FAILED" in h.executor.rollback_plan(t).render()


def test_audit_log_redacts_arguments(make_harness, tmp_path):
    h = make_harness()
    t = task(h, workspace=tmp_path)
    h.executor.run("fs_read", {"path": "nope.txt", "token": "ghp_" + "a" * 36}, t)
    rec = [r for r in h.audit.records if r["event"] == "tool_call"][-1]
    assert "ghp_" + "a" * 36 not in str(rec)
    text = (h.config.agent_dir / "audit" / "audit.jsonl").read_text()
    assert "ghp_" + "a" * 36 not in text and "tool_call" in text


def test_transient_network_failures_do_not_retry_mutations(make_harness):
    h = make_harness(flags={"k8s_unreachable": True})
    t = task(h)
    r = h.executor.run("kubectl_get", {"kind": "pods"}, t)
    assert not r.ok and r.failure_kind == "network" and "connectivity" in r.advice
    calls = [rec for rec in h.audit.records if rec["event"] == "tool_call"]
    assert len(calls) == 1  # retries are internal; one audited call


def test_timeout_is_reported_as_timeout(make_harness):
    h = make_harness(flags={"tool_timeout": True})
    t = task(h)
    r = h.executor.run("linux_uptime", {}, t)
    assert not r.ok and r.failure_kind == "timeout"
