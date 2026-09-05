import yaml

from agent.models import Environment, OperatingMode, PermissionLevel, RiskLevel, ToolSpec
from agent.policies.engine import Policy, PolicyEngine, load_policy


def spec(name, permission, category="kubernetes", requires_approval=False, risk=RiskLevel.LOW):
    return ToolSpec(name=name, description=name, permission=permission, category=category, requires_approval=requires_approval, risk_level=risk,
                    mutating=permission >= PermissionLevel.MODIFY)


READ = spec("kubectl_get", PermissionLevel.READ)
APPLY = spec("kubectl_apply", PermissionLevel.DEPLOY, requires_approval=True, risk=RiskLevel.HIGH)
DELETE = spec("kubectl_delete", PermissionLevel.DESTROY, requires_approval=True, risk=RiskLevel.CRITICAL)
FS_WRITE = spec("fs_write", PermissionLevel.MODIFY, category="filesystem")
PUSH = spec("git_push", PermissionLevel.MODIFY, category="git", requires_approval=True)


def engine(project_root=None):
    return PolicyEngine(load_policy(project_root))


def test_read_only_mode_allows_reads_and_blocks_mutations():
    e = engine()
    assert e.evaluate(READ, {}, environment=Environment.PRODUCTION, mode=OperatingMode.READ_ONLY).allowed
    d = e.evaluate(APPLY, {}, environment=Environment.LOCAL, mode=OperatingMode.READ_ONLY)
    assert not d.allowed and "read-only" in d.reason


def test_plan_mode_allows_analyze_only():
    e = engine()
    plan_tool = spec("terraform_plan", PermissionLevel.ANALYZE, category="terraform")
    assert e.evaluate(plan_tool, {}, environment=Environment.PRODUCTION, mode=OperatingMode.PLAN).allowed
    assert not e.evaluate(FS_WRITE, {}, environment=Environment.LOCAL, mode=OperatingMode.PLAN).allowed


def test_production_requires_explicit_approval_for_every_mutation():
    d = engine().evaluate(APPLY, {}, environment=Environment.PRODUCTION, mode=OperatingMode.AUTONOMOUS)
    assert d.allowed and d.requires_approval and d.explicit_confirmation


def test_unknown_environment_is_as_strict_as_production():
    d = engine().evaluate(FS_WRITE, {}, environment=Environment.UNKNOWN, mode=OperatingMode.AUTONOMOUS)
    # filesystem is a workspace tool: local policy applies, so it is auto-allowed even when the env is unknown
    assert d.allowed and not d.requires_approval
    infra = spec("kubectl_rollout_restart", PermissionLevel.DEPLOY)
    d2 = engine().evaluate(infra, {}, environment=Environment.UNKNOWN, mode=OperatingMode.AUTONOMOUS)
    assert d2.requires_approval and d2.explicit_confirmation


def test_local_autonomous_auto_allows_modify_and_deploy_but_not_destroy():
    e = engine()
    assert not e.evaluate(spec("kubectl_rollout_restart", PermissionLevel.DEPLOY), {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS).requires_approval
    d = e.evaluate(DELETE, {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS)
    assert d.requires_approval and d.explicit_confirmation


def test_approval_mode_requires_approval_for_all_mutations():
    d = engine().evaluate(FS_WRITE, {}, environment=Environment.LOCAL, mode=OperatingMode.APPROVAL)
    assert d.allowed and d.requires_approval


def test_protected_branch_push_is_refused_even_with_approval():
    e = engine()
    for branch in ("main", "master", "production", "release/1.2"):
        d = e.evaluate(PUSH, {"branch": branch}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS, target_branch=branch)
        assert not d.allowed and "protected" in d.reason
    assert e.evaluate(PUSH, {"branch": "fix/X-1"}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS, target_branch="fix/X-1").allowed


def test_shell_commands_are_classified_and_forbidden_ones_blocked():
    e = engine()
    shell = spec("shell_run", PermissionLevel.READ, category="linux")
    assert e.evaluate(shell, {}, environment=Environment.PRODUCTION, mode=OperatingMode.READ_ONLY, command="kubectl get pods").allowed
    d = e.evaluate(shell, {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS, command="rm -rf /")
    assert not d.allowed and "forbidden" in d.reason
    d = e.evaluate(shell, {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS, command="terraform destroy")
    assert d.allowed and d.requires_approval and d.permission == PermissionLevel.DESTROY and d.explicit_confirmation


def test_project_policy_can_only_be_stricter(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "policy.yaml").write_text(yaml.safe_dump({
        "environments": {"local": {"auto_allow_max_permission": "DESTROY", "explicit_confirmation": False}, "dev": {"auto_allow_max_permission": "READ"}},
        "protected_branches": ["develop"], "forbidden_tools": ["aws_destroy"], "tool_overrides": {"fs_write": {"requires_approval": True}},
        "forbidden": [], "limits": {"max_tool_calls": 10},
    }), encoding="utf-8")
    p = load_policy(tmp_path)
    assert p.env(Environment.LOCAL).auto_allow_max_permission == PermissionLevel.DEPLOY  # not relaxed to DESTROY
    assert p.env(Environment.DEV).auto_allow_max_permission == PermissionLevel.READ  # tightened
    assert p.is_protected_branch("develop") and p.is_protected_branch("main")
    assert p.is_forbidden_tool("aws_destroy")
    assert p.tool_overrides["fs_write"]["requires_approval"] is True
    assert p.limits["max_tool_calls"] == 10
    e = PolicyEngine(p)
    assert not e.evaluate(spec("aws_destroy", PermissionLevel.DESTROY, category="aws"), {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS).allowed
    assert e.evaluate(FS_WRITE, {}, environment=Environment.LOCAL, mode=OperatingMode.AUTONOMOUS).requires_approval


def test_merge_stricter_ignores_unknown_keys():
    merged = Policy.merge_stricter({"version": 1, "environments": {}, "protected_branches": []}, {"evil_override": True, "protected_branches": ["x"]})
    assert "evil_override" not in merged and merged["protected_branches"] == ["x"]
