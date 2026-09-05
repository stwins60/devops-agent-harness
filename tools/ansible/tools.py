"""Ansible tools: check mode first, real run only with approval."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command


class AnsibleBackend(Protocol):
    def playbook(self, workdir: Path, playbook: str, inventory: str, check: bool, extra: list[str]) -> dict[str, Any]: ...
    def inventory(self, workdir: Path, inventory: str) -> str: ...
    def lint(self, workdir: Path, playbook: str) -> dict[str, Any]: ...


class AnsibleCliBackend:
    def playbook(self, workdir, playbook, inventory, check, extra):
        argv = ["ansible-playbook", playbook, "-i", inventory] + (["--check", "--diff"] if check else []) + list(extra)
        out = run_command(argv, cwd=workdir, timeout=1800, env_passthrough=("ANSIBLE_CONFIG", "ANSIBLE_VAULT_PASSWORD_FILE", "SSH_AUTH_SOCK"))
        return {"returncode": out.returncode, "stdout": out.stdout, "stderr": out.stderr, "timed_out": out.timed_out}

    def inventory(self, workdir, inventory):
        out = run_command(["ansible-inventory", "-i", inventory, "--list"], cwd=workdir, timeout=120)
        if not out.ok:
            raise ToolError(f"ansible-inventory failed: {out.stderr[:400]}", kind="invalid")
        return out.stdout

    def lint(self, workdir, playbook):
        out = run_command(["ansible-lint", playbook], cwd=workdir, timeout=300)
        return {"returncode": out.returncode, "stdout": out.stdout, "stderr": out.stderr}


class MockAnsibleBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def playbook(self, workdir, playbook, inventory, check, extra):
        if not check:
            self.world.record("ansible_run", playbook=playbook, inventory=inventory)
            return {"returncode": 0, "stdout": self.world.ansible["run_output"], "stderr": "", "timed_out": False}
        return {"returncode": 0, "stdout": self.world.ansible["check_output"], "stderr": "", "timed_out": False}

    def inventory(self, workdir, inventory):
        return '{"web": {"hosts": ["api-host-01"]}, "_meta": {"hostvars": {}}}'

    def lint(self, workdir, playbook):
        return {"returncode": 0, "stdout": "Passed: 0 failure(s), 0 warning(s) on 1 files.\n", "stderr": ""}


def parse_recap(text: str) -> dict[str, dict[str, int]]:
    recap: dict[str, dict[str, int]] = {}
    for m in re.finditer(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", text, re.M):
        recap[m.group(1)] = {"ok": int(m.group(2)), "changed": int(m.group(3)), "unreachable": int(m.group(4)), "failed": int(m.group(5))}
    return recap


def _dir(args: dict[str, Any], ctx: ToolContext) -> Path:
    return Path(args.get("dir") or ctx.workspace or ctx.project_root or ".")


@tool("ansible_check", "Run a playbook in check + diff mode (no changes made).", category="ansible", permission=PermissionLevel.ANALYZE,
      permissions=["ansible.check"], timeout=1800,
      input_schema={"type": "object", "properties": {"playbook": {"type": "string"}, "inventory": {"type": "string"}, "dir": {"type": "string"}, "limit": {"type": "string"}},
                    "required": ["playbook", "inventory"]})
def ansible_check(args, ctx):
    extra = ["--limit", args["limit"]] if args.get("limit") else []
    res = ctx.backend("ansible").playbook(_dir(args, ctx), args["playbook"], args["inventory"], True, extra)
    recap = parse_recap(res["stdout"])
    ok = res["returncode"] == 0 and not res.get("timed_out")
    return ToolResult(ok=ok, output={"recap": recap, "stdout": res["stdout"][-8000:], "stderr": res["stderr"][-2000:]},
                      error=None if ok else (res["stderr"] or res["stdout"])[-800:], tool="ansible_check", args=args)


@tool("ansible_inventory", "List inventory hosts and groups.", category="ansible", permissions=["ansible.read"],
      input_schema={"type": "object", "properties": {"inventory": {"type": "string"}, "dir": {"type": "string"}}, "required": ["inventory"]})
def ansible_inventory(args, ctx):
    return {"inventory": ctx.backend("ansible").inventory(_dir(args, ctx), args["inventory"])}


@tool("ansible_lint", "Lint a playbook.", category="ansible", permission=PermissionLevel.ANALYZE, permissions=["ansible.read"],
      input_schema={"type": "object", "properties": {"playbook": {"type": "string"}, "dir": {"type": "string"}}, "required": ["playbook"]})
def ansible_lint(args, ctx):
    res = ctx.backend("ansible").lint(_dir(args, ctx), args["playbook"])
    return ToolResult(ok=res["returncode"] == 0, output=res, error=None if res["returncode"] == 0 else res["stdout"][-800:], tool="ansible_lint", args=args)


class AnsibleRunTool(Tool):
    spec = ToolSpec(name="ansible_run", description="Run a playbook for real (after a successful check run and approval).", permission=PermissionLevel.DEPLOY,
                    risk_level=RiskLevel.HIGH, requires_approval=True, permissions=["ansible.run"], category="ansible", mutating=True, timeout=1800,
                    rollback="re-run the playbook with the previous role/variable versions from git; not all tasks are reversible",
                    input_schema={"type": "object", "properties": {"playbook": {"type": "string"}, "inventory": {"type": "string"}, "dir": {"type": "string"}, "limit": {"type": "string"}},
                                  "required": ["playbook", "inventory"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        extra = ["--limit", args["limit"]] if args.get("limit") else []
        res = ctx.backend("ansible").playbook(_dir(args, ctx), args["playbook"], args["inventory"], False, extra)
        recap = parse_recap(res["stdout"])
        failed = any(v["failed"] or v["unreachable"] for v in recap.values())
        ok = res["returncode"] == 0 and not failed
        return ToolResult(ok=ok, output={"recap": recap, "stdout": res["stdout"][-8000:]}, error=None if ok else "playbook reported failures", tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [ansible_check, ansible_inventory, ansible_lint, AnsibleRunTool()]
