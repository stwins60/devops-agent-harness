"""Tests for Multi-Target Context, RBAC, Tamper-Resistant Audit, Policy-Testing, and Blast Radius."""
import tempfile
from pathlib import Path

from agent.audit.logger import AuditLogger
from agent.config import HarnessConfig
from agent.context.environment import resolve_environment
from agent.models import AgentIdentity, BlastRadiusLimits, Environment, OperatingMode, PermissionLevel, RiskLevel, ToolSpec
from agent.policies.engine import Policy, PolicyEngine
from agent.policies.testing import PolicyTestCase, PolicyTestSuite


def test_multi_context_config_and_env_resolution():
    data = {
        "aws_accounts": {"staging-account": "987654321098"},
        "kube_configs": {"prod-cluster": "/etc/kubernetes/prod.kubeconfig"},
        "environments": {
            "production": {
                "kube_contexts": ["prod-cluster"],
                "aws_accounts": ["123456789012"],
            },
            "staging": {
                "aws_accounts": ["987654321098"],
            }
        }
    }
    cfg = HarnessConfig()
    cfg._apply(data, source="config")

    res1 = resolve_environment(cfg, kube_context="prod-cluster")
    assert res1.environment == Environment.PRODUCTION

    res2 = resolve_environment(cfg, aws_account="staging-account")
    assert res2.environment == Environment.STAGING


def test_tamper_resistant_audit_logs():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "audit.jsonl"
        logger = AuditLogger(log_path)
        logger.log("stage", task="T-1", stage="init", status="ok")
        logger.tool_call(agent="specialist", task="T-1", tool="kubectl_get_pods", arguments={"namespace": "dev"},
                         risk="low", permission="READ", approval=False, result="success")
        logger.log("stage", task="T-1", stage="done", status="ok")

        # Verify pristine log
        ok, count, msg = AuditLogger.verify_integrity(log_path)
        assert ok is True
        assert count == 3

        # Tamper with file
        lines = log_path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace("kubectl_get_pods", "kubectl_delete_pods")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Verify tampered log fails verification
        ok_tampered, count_tampered, msg_tampered = AuditLogger.verify_integrity(log_path)
        assert ok_tampered is False
        assert count_tampered == 1


def test_rbac_and_target_scoping():
    policy_data = {
        "environments": {
            "dev": {"auto_allow_max_permission": "MODIFY", "require_approval": []},
        },
        "roles": {
            "developer": {
                "allowed_tools": ["kubectl_*", "git_*"],
                "max_permission": "MODIFY",
                "allowed_namespaces": ["dev-*", "sandbox"],
                "allowed_aws_accounts": ["111122223333"],
            }
        }
    }
    pol = Policy(policy_data)
    engine = PolicyEngine(pol)

    spec = ToolSpec(name="kubectl_apply", description="apply manifest", permission=PermissionLevel.MODIFY, mutating=True)
    dev_ident = AgentIdentity(roles=["developer"])

    # Allowed namespace
    dec1 = engine.evaluate(spec, {"namespace": "dev-app"}, environment=Environment.DEV, mode=OperatingMode.AUTONOMOUS, identity=dev_ident)
    assert dec1.allowed is True

    # Forbidden namespace by RBAC scope
    dec2 = engine.evaluate(spec, {"namespace": "prod-app"}, environment=Environment.DEV, mode=OperatingMode.AUTONOMOUS, identity=dev_ident)
    assert dec2.allowed is False
    assert "RBAC denial" in dec2.reason


def test_policy_test_suite_runner():
    policy_data = {
        "environments": {
            "dev": {"auto_allow_max_permission": "READ", "require_approval": ["kubectl_delete*"]},
        }
    }
    pol = Policy(policy_data)
    suite = PolicyTestSuite(pol)

    tc = PolicyTestCase(
        name="test_delete_requires_approval",
        tool="kubectl_delete_pod",
        environment="dev",
        mode="approval",
        expected_allowed=True,
        expected_requires_approval=True,
    )
    res = suite.run_case(tc)
    assert res.passed is True


def test_blast_radius_limits():
    policy_data = {
        "environments": {
            "dev": {"auto_allow_max_permission": "MODIFY", "require_approval": []}
        }
    }
    pol = Policy(policy_data)
    engine = PolicyEngine(pol)
    limits = BlastRadiusLimits(max_mutating_calls_per_window=2, window_seconds=60)

    spec = ToolSpec(name="kubectl_apply", description="apply manifest", permission=PermissionLevel.MODIFY, mutating=True)

    d1 = engine.evaluate(spec, {}, environment=Environment.DEV, mode=OperatingMode.AUTONOMOUS, blast_radius=limits)
    assert d1.allowed is True

    d2 = engine.evaluate(spec, {}, environment=Environment.DEV, mode=OperatingMode.AUTONOMOUS, blast_radius=limits)
    assert d2.allowed is True

    # 3rd mutation exceeds max 2 per window
    d3 = engine.evaluate(spec, {}, environment=Environment.DEV, mode=OperatingMode.AUTONOMOUS, blast_radius=limits)
    assert d3.allowed is False
    assert "blast radius limit reached" in d3.reason
