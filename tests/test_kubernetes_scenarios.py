import pytest

from agent.models import TaskStatus


@pytest.mark.parametrize("scenario,expected,confidence", [
    ("probe-port-mismatch", "Probe port mismatch", 0.9),
    ("oom", "OOMKilled", 0.9),
    ("image-pull", "Image pull failure", 0.9),
    ("pending", "cannot be scheduled", 0.9),
    ("config-error", "ConfigMap key 'DB_URL' is missing", 0.9),
    ("healthy", "No workload fault detected", 0.8),
])
def test_kubernetes_scenarios_produce_evidence_backed_conclusions(make_harness, scenario, expected, confidence):
    h = make_harness(scenario=scenario)
    t = h.run("Why is my Kubernetes API deployment failing?")
    assert t.status == TaskStatus.COMPLETED
    assert t.diagnosis and t.diagnosis.conclusion and expected in t.diagnosis.conclusion
    assert t.diagnosis.confidence >= confidence
    # every conclusion must be backed by facts gathered through tools
    assert len(t.facts()) >= 3 and all(f.source for f in t.facts())
    assert h.world.mutations == []  # diagnosis is read-only
    assert not any(call.tool.startswith(("kubectl_apply", "kubectl_delete", "kubectl_rollout_restart", "kubectl_rollout_undo")) for call in t.tool_calls)


def test_exit_137_without_oom_reason_is_not_called_oom(make_harness):
    t = make_harness(scenario="probe-port-mismatch").run("why is my pod crashing?")
    oom = [h for h in t.diagnosis.hypotheses if "137" in h.statement]
    assert oom and oom[0].status == "rejected"


def test_diagnosis_uses_runbook_and_records_recommendation(make_harness):
    t = make_harness().run("pod api is in CrashLoopBackOff in production namespace")
    assert any("kubernetes-crashloopbackoff" in e.statement for e in t.evidence)
    assert any("readinessProbe port to 8080" in r for r in t.diagnosis.recommendations)


def test_explicit_target_and_namespace_are_honoured(make_harness):
    t = make_harness().run("diagnose kubernetes deployment/api -n production")
    assert t.context["targets"]["deployment"] == "api" and t.context["targets"]["namespace"] == "production"
    missing = make_harness().run("diagnose kubernetes deployment/ghost -n production")
    assert any("was not found" in f.statement for f in missing.facts())
