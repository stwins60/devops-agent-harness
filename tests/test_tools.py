import pytest
import yaml

from agent.models import PermissionLevel, ToolResult
from tools.base import ToolContext, ToolError
from tools.catalog import build_registry
from tools.filesystem.tools import FsReplaceTool, FsWriteTool, fs_read, fs_search
from tools.terraform.tools import analyze_plan
from tools.ansible.tools import parse_recap
from tools.cicd.tools import analyze_log


def test_registry_contains_phase1_tools_and_exports_manifest(make_config):
    reg = build_registry(make_config())
    for name in ("fs_read", "fs_write", "git_status", "git_push", "github_create_pr", "gitlab_create_mr", "jira_get_issue", "jira_transition", "docker_ps",
                 "kubectl_get", "kubectl_apply", "kubectl_delete", "linux_disk_usage", "net_dns_lookup", "aws_describe", "aws_modify", "terraform_plan",
                 "terraform_apply", "terraform_destroy", "ansible_check", "cicd_job_logs", "obs_prometheus_query", "sec_secret_scan", "shell_run"):
        assert name in reg, name
    manifest = yaml.safe_load(reg.to_yaml())
    entry = next(m for m in manifest if m["name"] == "terraform_plan")
    assert entry["risk_level"] == "low" and entry["requires_approval"] is False and entry["permission"] == "ANALYZE" and entry["timeout"] == 300
    assert "terraform.plan" in entry["permissions"]


def test_every_mutating_tool_declares_modify_or_higher_and_rollback(make_config):
    reg = build_registry(make_config())
    for t in reg:
        s = t.spec
        if s.mutating:
            assert s.permission >= PermissionLevel.MODIFY, s.name
            assert s.rollback or type(t).rollback is not type(t).__mro__[-2].rollback, f"{s.name} has no rollback strategy"
        if s.permission >= PermissionLevel.DESTROY:
            assert s.requires_approval, s.name
        assert s.input_schema.get("type", "object") == "object"


def test_model_tool_definitions_filter_by_permission(make_config):
    reg = build_registry(make_config())
    names = {d["name"] for d in reg.model_tool_definitions(max_permission=PermissionLevel.ANALYZE)}
    assert "kubectl_get" in names and "terraform_plan" in names and "kubectl_apply" not in names


def test_validate_args_reports_missing_and_wrong_types(make_config):
    reg = build_registry(make_config())
    t = reg.get("kubectl_logs")
    assert t.validate_args({}) == ["missing required argument 'pod'"]
    assert "must be integer" in t.validate_args({"pod": "x", "tail": "many"})[0]


def ctx(tmp_path, **kw):
    return ToolContext(workspace=tmp_path, project_root=tmp_path, backends={}, **kw)


def test_filesystem_sandbox_blocks_escape(tmp_path):
    with pytest.raises(ToolError) as exc:
        fs_read.run({"path": "../../etc/passwd"}, ctx(tmp_path))
    assert exc.value.kind == "permission"


def test_fs_write_refuses_secrets_and_supports_rollback(tmp_path):
    w = FsWriteTool()
    with pytest.raises(ToolError):
        w.run({"path": "cfg.env", "content": "password=hunter2"}, ctx(tmp_path))
    r1 = w.run({"path": "a.txt", "content": "one\n"}, ctx(tmp_path))
    assert r1.ok and (tmp_path / "a.txt").read_text() == "one\n" and r1.output["created"]
    r2 = w.run({"path": "a.txt", "content": "two\n"}, ctx(tmp_path))
    assert "-one" in r2.output["diff"] and "+two" in r2.output["diff"]
    rb = w.rollback({"path": "a.txt"}, r2, ctx(tmp_path))
    assert rb.ok and (tmp_path / "a.txt").read_text() == "one\n"
    rb1 = w.rollback({"path": "a.txt"}, r1, ctx(tmp_path))
    assert rb1.ok and not (tmp_path / "a.txt").exists()


def test_fs_replace_requires_unique_match_and_dry_run(tmp_path):
    (tmp_path / "d.yaml").write_text("port: 8000\nport: 8000\n")
    r = FsReplaceTool()
    with pytest.raises(ToolError):
        r.run({"path": "d.yaml", "old": "port: 8000", "new": "port: 8080"}, ctx(tmp_path))
    dry = r.run({"path": "d.yaml", "old": "port: 8000", "new": "port: 8080", "replace_all": True}, ctx(tmp_path, dry_run=True))
    assert dry.dry_run and "port: 8000" in (tmp_path / "d.yaml").read_text()
    real = r.run({"path": "d.yaml", "old": "port: 8000", "new": "port: 8080", "replace_all": True}, ctx(tmp_path))
    assert real.ok and real.output["replacements"] == 2 and "8000" not in (tmp_path / "d.yaml").read_text()
    with pytest.raises(ToolError) as exc:
        r.run({"path": "d.yaml", "old": "missing", "new": "x"}, ctx(tmp_path))
    assert exc.value.kind == "not_found"


def test_fs_search_redacts_hits(tmp_path):
    (tmp_path / "x.txt").write_text("token=abcdef123456\nhello\n")
    res = fs_search.run({"pattern": "token"}, ctx(tmp_path))
    assert res.ok and "abcdef123456" not in res.output["hits"][0]["text"]


def test_terraform_plan_analysis_risk_levels():
    a = analyze_plan("Plan: 0 to add, 1 to change, 0 to destroy.")
    assert a["risk"] == "medium" and a["change"] == 1
    b = analyze_plan("  # aws_db_instance.main will be destroyed\nPlan: 0 to add, 0 to change, 1 to destroy.")
    assert b["risk"] == "critical" and b["destroy"] == 1 and "aws_db_instance.main" in b["sensitive_resources"]
    assert analyze_plan("No changes. Your infrastructure matches the configuration.")["no_changes"]


def test_ansible_recap_and_ci_log_analysis():
    recap = parse_recap("PLAY RECAP ***\nhost1 : ok=3 changed=1 unreachable=0 failed=0\n")
    assert recap["host1"]["changed"] == 1
    analysis = analyze_log("app.py:1:1: F401 'os' imported but unused\nError: Process completed with exit code 1.")
    labels = {f["label"] for f in analysis["findings"]}
    assert "lint: unused import" in labels and "non-zero exit" in labels


def test_mock_backends_raise_classified_errors(make_harness):
    h = make_harness(flags={"k8s_unreachable": True, "jira_unavailable": True, "aws_creds_expired": True})
    with pytest.raises(ToolError) as e1:
        h.backends["kubernetes"].list("pods", "production")
    assert e1.value.kind == "network"
    with pytest.raises(ToolError) as e2:
        h.backends["jira"].get_issue("DEVOPS-382")
    assert e2.value.kind == "network"
    with pytest.raises(ToolError) as e3:
        h.backends["aws"].identity()
    assert e3.value.kind == "auth"


def test_aws_describe_refuses_mutations(make_harness):
    h = make_harness()
    from agent.state.store import TaskState

    task = TaskState(id="T-1", request="x")
    res = h.executor.run("aws_describe", {"service": "ec2", "operation": "terminate-instances"}, task)
    assert not res.ok and res.failure_kind == "permission"
    ok = h.executor.run("aws_describe", {"service": "eks", "operation": "describe-cluster", "params": {"name": "mock-cluster"}}, task)
    assert ok.ok and ok.output["cluster"]["version"] == "1.28"
