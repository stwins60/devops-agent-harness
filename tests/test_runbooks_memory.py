import pytest

from agent.memory.store import MemoryError, MemoryStore
from agent.runbooks.loader import BUILTIN_RUNBOOK_DIR, RunbookLibrary, RunbookError, parse_runbook


def test_all_builtin_runbooks_parse():
    lib = RunbookLibrary([BUILTIN_RUNBOOK_DIR])
    assert lib.errors == []
    assert len(lib.runbooks) >= 12
    domains = {rb.domain for rb in lib.runbooks}
    assert {"kubernetes", "aws", "linux", "networking", "database", "cicd", "security", "docker"} <= domains
    for rb in lib.runbooks:
        assert rb.diagnosis and rb.remediation and rb.validation and rb.rollback


def test_runbook_matching():
    lib = RunbookLibrary([BUILTIN_RUNBOOK_DIR])
    assert lib.find("why is my pod crashing with crashloopbackoff")[0].name == "kubernetes-crashloopbackoff"
    assert lib.find("upgrade our Kubernetes worker nodes")[0].name == "kubernetes-worker-node-upgrade"
    assert lib.find("disk full no space left on device", domain="linux")[0].name == "linux-disk-full"
    assert lib.find("completely unrelated text about cooking") == []


def test_runbook_schema_validation(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ndescription: y\n")
    with pytest.raises(RunbookError) as exc:
        parse_runbook(bad)
    assert "missing required fields" in str(exc.value)
    lib = RunbookLibrary([tmp_path])
    assert lib.errors and not lib.runbooks


def test_runbook_render_and_steps():
    lib = RunbookLibrary([BUILTIN_RUNBOOK_DIR])
    rb = lib.get("kubernetes-crashloopbackoff")
    text = rb.render()
    assert "Prechecks" in text and "kubectl_rollout_undo" in text
    assert any(s.tool == "kubectl_logs" and s.args.get("previous") for s in rb.diagnosis)


def test_memory_store_roundtrip_and_recall(tmp_path):
    mem = MemoryStore(tmp_path / ".agent")
    mem.remember("decisions", "Use ArgoCD for production deploys", "We deploy the api service through ArgoCD from the k8s/ folder.", tags=["gitops"])
    mem.remember("conventions", "Branch naming", "fix/<TICKET>-slug", tags=["git"])
    hits = mem.recall("how do we deploy the api with argocd")
    assert hits and hits[0].title == "Use ArgoCD for production deploys"
    assert mem.load("conventions", "branch-naming").content.strip().endswith("fix/<TICKET>-slug")
    assert "ArgoCD" in mem.context_summary("argocd deploy")


def test_memory_refuses_secrets_and_unknown_categories(tmp_path):
    mem = MemoryStore(tmp_path / ".agent")
    with pytest.raises(MemoryError):
        mem.remember("memory", "db creds", "password=supersecret123")
    with pytest.raises(MemoryError):
        mem.remember("nope", "x", "y")
    assert mem.list() == []
