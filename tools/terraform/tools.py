"""Terraform tools: CLI backend + mock backend, with plan risk analysis."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command


class TerraformBackend(Protocol):
    def run(self, workdir: Path, *args: str, timeout: int = 300) -> dict[str, Any]: ...


class TerraformCliBackend:
    def run(self, workdir: Path, *args: str, timeout: int = 300) -> dict[str, Any]:
        out = run_command(["terraform", *args, "-no-color"] if args[0] not in ("fmt",) else ["terraform", *args], cwd=workdir, timeout=timeout,
                          env_passthrough=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "TF_TOKEN_app_terraform_io"))
        if out.timed_out:
            raise ToolError(f"terraform {args[0]} timed out after {timeout}s", kind="timeout")
        return {"returncode": out.returncode, "stdout": out.stdout, "stderr": out.stderr}


class MockTerraformBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def run(self, workdir: Path, *args: str, timeout: int = 300) -> dict[str, Any]:
        cmd = args[0]
        tf = self.world.terraform
        if cmd == "fmt":
            return {"returncode": 0 if tf.get("fmt_ok", True) else 3, "stdout": "" if tf.get("fmt_ok", True) else "main.tf\n", "stderr": ""}
        if cmd == "validate":
            return {"returncode": 0 if tf.get("validate_ok", True) else 1, "stdout": "Success! The configuration is valid.\n" if tf.get("validate_ok", True) else "",
                    "stderr": "" if tf.get("validate_ok", True) else "Error: Unsupported argument\n  on main.tf line 12: An argument named \"verison\" is not expected here.\n"}
        if cmd == "plan":
            if self.world.flags.get("terraform_plan_fails"):
                return {"returncode": 1, "stdout": "", "stderr": "Error: error configuring Terraform AWS Provider: no valid credential sources found\n"}
            return {"returncode": 2 if "-detailed-exitcode" in args else 0, "stdout": tf["plan_output"], "stderr": ""}
        if cmd == "apply":
            self.world.record("terraform_apply", workdir=str(workdir))
            return {"returncode": 0, "stdout": "Apply complete! Resources: 0 added, 1 changed, 0 destroyed.\n", "stderr": ""}
        if cmd == "destroy":
            self.world.record("terraform_destroy", workdir=str(workdir))
            return {"returncode": 0, "stdout": "Destroy complete! Resources: 4 destroyed.\n", "stderr": ""}
        if cmd == "show":
            return {"returncode": 0, "stdout": tf["plan_output"], "stderr": ""}
        if cmd == "state":
            return {"returncode": 0, "stdout": "module.eks.aws_eks_cluster.this\nmodule.eks.aws_eks_node_group.workers_a\n", "stderr": ""}
        if cmd == "init":
            return {"returncode": 0, "stdout": "Terraform has been successfully initialized!\n", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}


def analyze_plan(text: str) -> dict[str, Any]:
    m = re.search(r"Plan:\s*(\d+) to add,\s*(\d+) to change,\s*(\d+) to destroy", text)
    add, change, destroy = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    replaced = len(re.findall(r"must be replaced", text))
    resources = re.findall(r"#\s+(\S+)\s+will be (created|updated in-place|destroyed|replaced)", text)
    sensitive = [r for r, _ in resources if re.search(r"iam|security_group|db_instance|rds|kms|route53|network_acl", r)]
    risk = RiskLevel.LOW
    if change or add:
        risk = RiskLevel.MEDIUM
    if destroy or replaced or sensitive:
        risk = RiskLevel.HIGH
    if destroy > 3 or any("db_instance" in s or "rds" in s for s in sensitive):
        risk = RiskLevel.CRITICAL
    return {"add": add, "change": change, "destroy": destroy, "replaced": replaced, "resources": [{"address": a, "action": b} for a, b in resources],
            "sensitive_resources": sensitive, "risk": risk.value, "no_changes": "No changes." in text or (not m and not resources)}


def _dir(args: dict[str, Any], ctx: ToolContext) -> Path:
    raw = args.get("dir") or ctx.workspace or ctx.project_root or "."
    p = Path(raw)
    if not p.exists():
        raise ToolError(f"terraform directory not found: {p}", kind="not_found")
    return p


def _tf_read(name: str, description: str, argv: list[str], permission: PermissionLevel = PermissionLevel.READ, timeout: int = 120):
    def fn(args, ctx):
        res = ctx.backend("terraform").run(_dir(args, ctx), *argv, timeout=timeout)
        ok = res["returncode"] == 0
        return ToolResult(ok=ok, output=res, error=None if ok else (res["stderr"] or res["stdout"])[:800], tool=name, args=args,
                          failure_kind=None if ok else "invalid")
    return tool(name, description, category="terraform", permission=permission, permissions=["terraform.read"],
                input_schema={"type": "object", "properties": {"dir": {"type": "string"}}}, timeout=timeout)(fn)


terraform_fmt_check = _tf_read("terraform_fmt_check", "Check formatting (terraform fmt -check -recursive).", ["fmt", "-check", "-recursive"])
terraform_validate = _tf_read("terraform_validate", "Validate configuration syntax and consistency.", ["validate"], PermissionLevel.ANALYZE)
terraform_show = _tf_read("terraform_show", "Show the current state or a saved plan.", ["show"])
terraform_state_list = _tf_read("terraform_state_list", "List resources in state.", ["state", "list"])


@tool("terraform_plan", "Generate an execution plan and analyse its risk (adds/changes/destroys, sensitive resources).", category="terraform",
      permission=PermissionLevel.ANALYZE, permissions=["filesystem.read", "terraform.plan"], timeout=300,
      input_schema={"type": "object", "properties": {"dir": {"type": "string"}, "vars": {"type": "object"}}})
def terraform_plan(args, ctx):
    argv = ["plan", "-input=false", "-detailed-exitcode", "-lock=false"]
    for k, v in (args.get("vars") or {}).items():
        argv += ["-var", f"{k}={v}"]
    res = ctx.backend("terraform").run(_dir(args, ctx), *argv, timeout=300)
    if res["returncode"] == 1:
        return ToolResult(ok=False, output=res, error=(res["stderr"] or res["stdout"])[:800], tool="terraform_plan", args=args,
                          failure_kind="auth" if "credential" in (res["stderr"] or "").lower() else "invalid")
    analysis = analyze_plan(res["stdout"])
    return {"plan": res["stdout"][-12000:], "analysis": analysis, "changes_present": res["returncode"] == 2 or not analysis["no_changes"]}


class TerraformApplyTool(Tool):
    spec = ToolSpec(name="terraform_apply", description="Apply the Terraform plan. Requires approval; destroys must be explicitly acknowledged.",
                    permission=PermissionLevel.DEPLOY, risk_level=RiskLevel.HIGH, requires_approval=True, permissions=["terraform.apply"], category="terraform",
                    mutating=True, timeout=1800, rollback="restore the previous configuration from git, run terraform plan, then terraform apply",
                    input_schema={"type": "object", "properties": {"dir": {"type": "string"}, "vars": {"type": "object"}, "allow_destroy": {"type": "boolean"}}})

    def run(self, args, ctx):
        be = ctx.backend("terraform")
        d = _dir(args, ctx)
        plan = be.run(d, "plan", "-input=false", "-lock=false", timeout=300)
        analysis = analyze_plan(plan["stdout"])
        if analysis["destroy"] and not args.get("allow_destroy"):
            raise ToolError(f"plan destroys {analysis['destroy']} resource(s); pass allow_destroy=true after explicit human approval", kind="permission")
        if ctx.dry_run:
            return ToolResult(ok=True, output={"dry_run": True, "analysis": analysis}, tool=self.name, args=args, dry_run=True)
        argv = ["apply", "-input=false", "-auto-approve"]
        for k, v in (args.get("vars") or {}).items():
            argv += ["-var", f"{k}={v}"]
        res = be.run(d, *argv, timeout=1800)
        ok = res["returncode"] == 0
        return ToolResult(ok=ok, output={"analysis": analysis, **res}, error=None if ok else res["stderr"][:800], tool=self.name, args=args)


class TerraformDestroyTool(Tool):
    spec = ToolSpec(name="terraform_destroy", description="Destroy all managed infrastructure. NEVER automatic; explicit approval required.",
                    permission=PermissionLevel.DESTROY, risk_level=RiskLevel.CRITICAL, requires_approval=True, permissions=["terraform.destroy"],
                    category="terraform", mutating=True, timeout=1800, rollback="NOT AVAILABLE: re-create from configuration (data loss possible)",
                    input_schema={"type": "object", "properties": {"dir": {"type": "string"}, "target": {"type": "string"}}})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        argv = ["destroy", "-input=false", "-auto-approve"] + (["-target", args["target"]] if args.get("target") else [])
        res = ctx.backend("terraform").run(_dir(args, ctx), *argv, timeout=1800)
        ok = res["returncode"] == 0
        return ToolResult(ok=ok, output=res, error=None if ok else res["stderr"][:800], tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [terraform_fmt_check, terraform_validate, terraform_show, terraform_state_list, terraform_plan, TerraformApplyTool(), TerraformDestroyTool()]
