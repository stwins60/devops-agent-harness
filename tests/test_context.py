from agent.config import EnvironmentBinding, HarnessConfig
from agent.context.agents_md import discover
from agent.context.environment import infer_hints, resolve_environment
from agent.models import Environment
from agent.orchestrator.understanding import understand, extract_targets, detect_domains
from agent.models import TaskKind


def test_agents_md_hierarchy_most_specific_wins(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Root\n\n## Conventions\n\nroot rule\n\n## Testing\n\nroot tests\n")
    svc = tmp_path / "services" / "api"
    svc.mkdir(parents=True)
    (svc / "AGENTS.md").write_text("# API\n\n## Conventions\n\napi rule\n")
    (tmp_path / ".git").mkdir()
    s = discover(svc / "main.py", root=tmp_path)
    assert [f.depth for f in s.files] == [0, 2]
    assert s.section("conventions") == "api rule"
    assert s.section("testing") == "root tests"
    merged = s.merged()
    assert merged.index("root rule") < merged.index("api rule")


def test_environment_bindings_beat_declared_and_hints_only_escalate(project_root):
    cfg = HarnessConfig.load(project_root, {"environment": "dev"})
    cfg.environment_bindings = [EnvironmentBinding("production", kube_contexts=["prod-eks"]), EnvironmentBinding("staging", namespaces=["staging"])]
    r = resolve_environment(cfg, kube_context="prod-eks")
    assert r.environment == Environment.PRODUCTION and r.source == "binding"
    r2 = resolve_environment(cfg, kube_context="kind-local")
    assert r2.environment == Environment.DEV and r2.source == "flag"
    r3 = resolve_environment(cfg, kube_context="kind-local", untrusted_hints=["production"])
    assert r3.environment == Environment.PRODUCTION and "hint" in r3.source
    r4 = resolve_environment(cfg, kube_context="prod-eks", untrusted_hints=["dev"])
    assert r4.environment == Environment.PRODUCTION  # hints can never relax


def test_unverified_environment_defaults_to_unknown_strict(project_root):
    cfg = HarnessConfig.load(project_root)
    r = resolve_environment(cfg)
    assert r.environment == Environment.UNKNOWN and not r.verified and r.environment.strictness == Environment.PRODUCTION.strictness


def test_config_layering_and_env_vars(project_root, monkeypatch):
    (project_root / ".agent").mkdir()
    (project_root / ".agent" / "config.yaml").write_text("mode: autonomous\nenvironment: staging\nlimits:\n  max_tool_calls: 5\nenvironments:\n  production:\n    kube_contexts: [prod]\n")
    monkeypatch.setenv("DEVOPS_AGENT_MODE", "plan")
    cfg = HarnessConfig.load(project_root, {"mock": True})
    assert cfg.mode.value == "plan" and cfg.environment == Environment.STAGING and cfg.limits.max_tool_calls == 5
    assert cfg.environment_for(kube_context="prod") == Environment.PRODUCTION and cfg.mock


def test_infer_hints_and_targets():
    assert "production" in infer_hints("deploy to production namespace")
    t = extract_targets("diagnose kubernetes deployment/api -n production for DEVOPS-12 see PR #421 run 994")
    assert t["deployment"] == "api" and t["namespace"] == "production" and t["ticket"] == "DEVOPS-12" and t["pr"] == 421 and t["run_id"] == 994
    assert "deployment" not in extract_targets("why is my deployment failing")


def test_understanding_kind_and_domains():
    u = understand("Fix DEVOPS-382")
    assert u.kind == TaskKind.JIRA and u.ticket == "DEVOPS-382"
    assert understand("production API is returning 503").kind == TaskKind.INCIDENT
    assert understand("plan the upgrade of our worker nodes").kind == TaskKind.PLAN
    assert understand("why is my pod crashing?").kind == TaskKind.DIAGNOSE
    assert "kubernetes" in detect_domains("kubectl says the pod is in CrashLoopBackOff")
    assert detect_domains("terraform plan shows destroy")[0] == "terraform"
