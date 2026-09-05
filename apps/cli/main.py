"""devops-agent command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from agent.config import HarnessConfig
from agent.models import OperatingMode, TaskKind, TaskStatus
from agent.rca.engine import RootCauseEngine
from agent.reports.render import render_plan

USAGE = """examples:
  devops-agent "why is my pod crashing?"
  devops-agent --mock "Why is my Kubernetes API deployment failing?"
  devops-agent jira DEVOPS-382 [--repo path] [--yes]
  devops-agent incident "production API is returning 503"
  devops-agent diagnose kubernetes deployment/api -n production
  devops-agent plan "upgrade our Kubernetes worker nodes"
  devops-agent fix DEVOPS-382 --dry-run
  devops-agent execute DEVOPS-382 --yes
  devops-agent resume DEVOPS-382
  devops-agent tasks list | tasks show TASK-ID | tools list | runbooks list | mcp-serve | init
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devops-agent", description="Model-agnostic DevOps AI agent harness", epilog=USAGE,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true", help="use mock backends (no credentials or infrastructure needed)")
    p.add_argument("--scenario", help="mock scenario: probe-port-mismatch|oom|image-pull|pending|config-error|healthy|ci-failure|disk-full")
    p.add_argument("--mode", choices=["read-only", "plan", "approval", "autonomous"], help="operating mode (default from config: approval)")
    p.add_argument("--env", help="declared environment (verified against trusted bindings; can only make policy stricter)")
    p.add_argument("--dry-run", action="store_true", help="investigate, plan and show diffs without mutating anything")
    p.add_argument("--yes", "-y", action="store_true", help="auto-approve non-explicit approvals (never DESTROY/production explicit confirmations)")
    p.add_argument("--approve-all", action="store_true", help="DANGEROUS: also auto-approve explicit confirmations (tests/demos only)")
    p.add_argument("--non-interactive", action="store_true", help="never prompt; approvals are denied and the task pauses (resumable)")
    p.add_argument("--provider", help="model provider: auto|mock|none|openai|anthropic|claude-code|opencode|copilot|ollama")
    p.add_argument("--model", help="model name for the provider")
    p.add_argument("--repo", help="repository path to operate on (task workspace)")
    p.add_argument("--project-root", help="project root containing .agent/ (default: cwd)")
    p.add_argument("--tasks-dir", help="directory for durable task state (default: <project>/tasks)")
    p.add_argument("--json", action="store_true", help="print the final task state as JSON")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress progress output")
    p.add_argument("--audit-echo", action="store_true", help="echo audit records to stdout")
    p.add_argument("--flag", action="append", default=[], help="mock failure flag (repeatable): jira_unavailable, k8s_unreachable, aws_creds_expired, git_push_rejected, pr_create_fails, terraform_plan_fails, tool_timeout, rollback_fails, partial_deploy, permission_denied")
    p.add_argument("command", nargs="?", help="jira|incident|diagnose|plan|fix|execute|resume|tasks|tools|runbooks|mcp-serve|init or free text")
    p.add_argument("args", nargs=argparse.REMAINDER)
    return p


def _config_from_args(a: argparse.Namespace) -> HarnessConfig:
    overrides: dict[str, Any] = {}
    if a.mock:
        overrides["mock"] = True
    if a.scenario:
        overrides["mock_scenario"] = a.scenario
    if a.mode:
        overrides["mode"] = a.mode
    if a.env:
        overrides["environment"] = a.env
    if a.dry_run:
        overrides["dry_run"] = True
    if a.provider:
        overrides["provider"] = a.provider
    if a.model:
        overrides["provider_model"] = a.model
    if a.tasks_dir:
        overrides["tasks_dir"] = a.tasks_dir
    if a.non_interactive or not sys.stdin.isatty():
        overrides["non_interactive"] = True
    if a.yes or a.approve_all:
        overrides["auto_approve"] = True
    cfg = HarnessConfig.load(Path(a.project_root) if a.project_root else None, overrides)
    if a.approve_all:
        cfg.extra["approve_all"] = True
    if a.flag:
        cfg.extra["mock_flags"] = {f: True for f in a.flag}
    return cfg


def _harness(cfg: HarnessConfig, echo: bool = False):
    from agent.harness import Harness
    from tools.mock.world import MockWorld

    world = MockWorld.build(cfg.mock_scenario, flags=cfg.extra.get("mock_flags")) if cfg.mock else None
    return Harness(cfg, world=world, echo_audit=echo)


def _print_result(task, quiet: bool, as_json: bool) -> None:
    if as_json:
        print(json.dumps(task.to_dict(), indent=2, default=str))
        return
    print()
    if task.diagnosis and task.kind in (TaskKind.QUESTION, TaskKind.DIAGNOSE):
        print(RootCauseEngine.render(task.diagnosis))
        print()
    if task.plan and task.kind == TaskKind.PLAN:
        print(render_plan(task.plan))
        print()
    if task.report:
        print(task.report)
    print()
    print(f"Task {task.id}: {task.status.value} at stage {task.stage.value}. Artifacts: tasks/{task.id}/")


def _run_task(cfg: HarnessConfig, request: str, kind: Optional[TaskKind], a: argparse.Namespace, task_id: Optional[str] = None) -> int:
    h = _harness(cfg, echo=a.audit_echo)
    progress = (lambda s: None) if a.quiet else (lambda s: print(s, flush=True))
    if not a.quiet:
        s = h.summary()
        print(f"devops-agent | mock={s['mock']}{' (' + str(s['scenario']) + ')' if s['scenario'] else ''} mode={cfg.mode.value} provider={s['provider']} tools={s['tools']}")
        if s["runbook_errors"]:
            print(f"warning: runbook errors: {s['runbook_errors']}")
    try:
        task = h.run(request, kind=kind, task_id=task_id, mode=OperatingMode.parse(a.mode) if a.mode else None, repo=Path(a.repo) if a.repo else None, progress=progress)
    finally:
        h.close()
    _print_result(task, a.quiet, a.json)
    return 0 if task.status in (TaskStatus.COMPLETED, TaskStatus.PAUSED, TaskStatus.WAITING_APPROVAL) else 1


def _resume(cfg: HarnessConfig, task_id: str, a: argparse.Namespace) -> int:
    h = _harness(cfg, echo=a.audit_echo)
    progress = (lambda s: None) if a.quiet else (lambda s: print(s, flush=True))
    try:
        task = h.resume(task_id, progress=progress)
    finally:
        h.close()
    _print_result(task, a.quiet, a.json)
    return 0 if task.status in (TaskStatus.COMPLETED, TaskStatus.PAUSED) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if not a.command:
        parser.print_help()
        return 2
    cmd, rest = a.command, list(a.args)
    cfg = _config_from_args(a)
    text = " ".join(rest).strip()

    if cmd == "jira":
        if not rest:
            print("usage: devops-agent jira ISSUE-KEY", file=sys.stderr)
            return 2
        return _run_task(cfg, f"Fix {rest[0]}" if len(rest) == 1 else text, TaskKind.JIRA, a, task_id=rest[0].upper())
    if cmd == "incident":
        return _run_task(cfg, text or "incident", TaskKind.INCIDENT, a)
    if cmd == "diagnose":
        return _run_task(cfg, f"diagnose {text}", TaskKind.DIAGNOSE, a)
    if cmd == "plan":
        return _run_task(cfg, text, TaskKind.PLAN, a)
    if cmd == "fix":
        return _run_task(cfg, f"Fix {text}", TaskKind.JIRA if rest and rest[0].upper() == rest[0] and "-" in rest[0] else TaskKind.FIX, a,
                         task_id=rest[0].upper() if rest and "-" in rest[0] and rest[0].upper() == rest[0] else None)
    if cmd in ("execute", "resume"):
        if not rest:
            print(f"usage: devops-agent {cmd} TASK-ID", file=sys.stderr)
            return 2
        task_id = rest[0]
        from agent.state.store import TaskStore

        store = TaskStore(cfg.tasks_dir)
        if store.exists(task_id):
            return _resume(cfg, task_id, a)
        if cmd == "execute":
            return _run_task(cfg, f"Fix {task_id}", TaskKind.JIRA, a, task_id=task_id.upper())
        print(f"task '{task_id}' not found in {cfg.tasks_dir}", file=sys.stderr)
        return 1
    if cmd == "tasks":
        return _tasks(cfg, rest)
    if cmd == "tools":
        return _tools(cfg, rest)
    if cmd == "runbooks":
        return _runbooks(cfg, rest)
    if cmd == "mcp-serve":
        from agent.approvals.engine import build_handler
        from agent.harness import Harness
        from agent.mcp.server import serve
        from tools.mock.world import MockWorld

        cfg.non_interactive = True
        world = MockWorld.build(cfg.mock_scenario, flags=cfg.extra.get("mock_flags")) if cfg.mock else None
        # no terminal behind an MCP server: only pre-approved tools (config mcp_preapproved) pass the approval gate
        handler = build_handler(interactive=False, auto_approve=False, preapproved=cfg.mcp_preapproved)
        return serve(Harness(cfg, world=world, approval_handler=handler))
    if cmd == "init":
        return _init(cfg)
    if cmd == "version":
        print("devops-agent 0.1.0")
        return 0
    # free text question
    request = (cmd + " " + text).strip()
    return _run_task(cfg, request, None, a)


def _tasks(cfg: HarnessConfig, rest: list[str]) -> int:
    from agent.state.store import TaskStore

    store = TaskStore(cfg.tasks_dir)
    sub = rest[0] if rest else "list"
    if sub == "list":
        rows = store.list()
        if not rows:
            print(f"no tasks in {cfg.tasks_dir}")
        for t in rows:
            print(f"{t.id:<22} {t.status.value:<18} {t.stage.value:<22} {t.kind.value:<9} {t.updated_at}  {t.request[:60]}")
        return 0
    if sub == "show" and len(rest) > 1:
        t = store.load(rest[1])
        print(json.dumps(t.to_dict(), indent=2, default=str))
        return 0
    print("usage: devops-agent tasks list | tasks show TASK-ID", file=sys.stderr)
    return 2


def _tools(cfg: HarnessConfig, rest: list[str]) -> int:
    from tools.catalog import build_registry

    reg = build_registry(cfg)
    if rest and rest[0] == "manifest":
        print(reg.to_yaml())
        return 0
    print(f"{'tool':<28} {'category':<14} {'permission':<9} {'risk':<9} approval  description")
    for t in reg:
        s = t.spec
        print(f"{s.name:<28} {s.category:<14} {s.permission.name:<9} {s.risk_level.value:<9} {'yes' if s.requires_approval else 'no':<9} {s.description[:70]}")
    return 0


def _runbooks(cfg: HarnessConfig, rest: list[str]) -> int:
    from agent.runbooks.loader import BUILTIN_RUNBOOK_DIR, RunbookLibrary

    lib = RunbookLibrary([BUILTIN_RUNBOOK_DIR, cfg.agent_dir / "runbooks"])
    if rest and rest[0] == "show" and len(rest) > 1:
        rb = lib.get(rest[1])
        print(rb.render() if rb else f"runbook '{rest[1]}' not found")
        return 0 if rb else 1
    if rest and rest[0] == "find" and len(rest) > 1:
        for rb in lib.find(" ".join(rest[1:])):
            print(f"{rb.name:<40} {rb.domain:<12} {rb.severity:<8} {rb.description[:60]}")
        return 0
    for rb in lib.runbooks:
        print(f"{rb.name:<40} {rb.domain:<12} {rb.severity:<8} {rb.description[:60]}")
    for e in lib.errors:
        print(f"error: {e}", file=sys.stderr)
    return 0


def _init(cfg: HarnessConfig) -> int:
    root = cfg.project_root
    (root / ".agent").mkdir(exist_ok=True)
    example = Path(__file__).resolve().parents[2] / "examples" / "config.example.yaml"
    target = root / ".agent" / "config.yaml"
    if not target.exists():
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {target}")
    agents = root / "AGENTS.md"
    if not agents.exists():
        agents.write_text("# AGENTS.md\n\n## Project architecture\n\n_describe repositories, services, deployment, infrastructure_\n\n"
                          "## Testing\n\n## CI/CD\n\n## Security rules\n\n_enforced by policies/default.yaml; document extra restrictions here_\n\n"
                          "## DevOps conventions\n\n## Agent behaviour\n\n## Tool permissions\n\n## Approval requirements\n", encoding="utf-8")
        print(f"wrote {agents}")
    for d in ("memory", "decisions", "runbooks", "architecture", "incidents", "conventions"):
        (root / ".agent" / d).mkdir(exist_ok=True)
    print("initialised .agent/ (memory, decisions, runbooks, architecture, incidents, conventions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
