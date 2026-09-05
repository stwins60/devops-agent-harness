"""AWS tools. Read-only operations go through ``aws_describe``; mutations through ``aws_modify`` (approval)."""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command

READ_PREFIXES = ("describe-", "list-", "get-", "lookup-", "search-", "query", "scan", "head-", "batch-get-", "filter-", "simulate-", "test-", "check-")
SUPPORTED_SERVICES = {"ec2", "ecs", "eks", "s3", "s3api", "iam", "sts", "elbv2", "elb", "route53", "cloudwatch", "logs", "lambda", "rds", "ecr",
                      "secretsmanager", "ssm", "cloudformation", "autoscaling", "sns", "sqs", "dynamodb", "kms", "acm", "cloudtrail", "ce", "pricing"}


class AwsBackend(Protocol):
    def identity(self) -> dict[str, Any]: ...
    def call(self, service: str, operation: str, params: dict[str, Any]) -> Any: ...


class AwsCliBackend:
    def __init__(self, profile: Optional[str] = None, region: Optional[str] = None, timeout: int = 90) -> None:
        self.profile, self.region, self.timeout = profile, region, timeout

    def _argv(self, service: str, operation: str, params: dict[str, Any]) -> list[str]:
        argv = ["aws", service, operation, "--output", "json"]
        if self.profile:
            argv += ["--profile", self.profile]
        if self.region:
            argv += ["--region", self.region]
        for k, v in params.items():
            flag = "--" + k.replace("_", "-")
            if isinstance(v, bool):
                argv.append(flag if v else f"--no-{k.replace('_', '-')}")
            elif isinstance(v, (list, dict)):
                argv += [flag, json.dumps(v)]
            else:
                argv += [flag, str(v)]
        return argv

    def identity(self) -> dict[str, Any]:
        return self.call("sts", "get-caller-identity", {})

    def call(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        out = run_command(self._argv(service, operation, params), timeout=self.timeout,
                          env_passthrough=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"))
        if not out.ok:
            msg = (out.stderr or out.stdout).strip()
            low = msg.lower()
            kind = "auth" if "expiredtoken" in low or "invalidclienttokenid" in low or "unable to locate credentials" in low else \
                   "permission" if "accessdenied" in low or "unauthorizedoperation" in low or "not authorized" in low else \
                   "rate_limit" if "throttling" in low else "not_found" if "notfound" in low or "does not exist" in low else "timeout" if out.timed_out else "unknown"
            raise ToolError(f"aws {service} {operation} failed: {msg[:600]}", kind=kind)
        try:
            return json.loads(out.stdout) if out.stdout.strip() else {}
        except json.JSONDecodeError:
            return {"raw": out.stdout}


class MockAwsBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def _check(self) -> None:
        if self.world.flags.get("aws_creds_expired"):
            raise ToolError("An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired", kind="auth")
        if self.world.flags.get("permission_denied"):
            raise ToolError("An error occurred (AccessDenied) when calling the DescribeInstances operation: User is not authorized to perform: ec2:DescribeInstances", kind="permission")

    def identity(self) -> dict[str, Any]:
        self._check()
        return dict(self.world.aws["identity"])

    def call(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        self._check()
        if (service, operation) in self.world.aws["responses"]:
            return self.world.aws["responses"][(service, operation)]
        if not operation.startswith(READ_PREFIXES):
            self.world.record("aws_modify", service=service, operation=operation, params=params)
            return {"ResponseMetadata": {"HTTPStatusCode": 200}, "mock": True, "operation": operation}
        return {"mock": True, "note": f"no fixture for {service} {operation}", "items": []}


def is_read_only(operation: str) -> bool:
    return operation.startswith(READ_PREFIXES)


def _validate(service: str, operation: str) -> None:
    if service not in SUPPORTED_SERVICES:
        raise ToolError(f"unsupported AWS service '{service}'", kind="invalid", advice=f"supported: {', '.join(sorted(SUPPORTED_SERVICES))}")
    if not re.match(r"^[a-z0-9\-]+$", operation):
        raise ToolError(f"invalid operation name '{operation}'", kind="invalid")


@tool("aws_identity", "Show the AWS account/role in use (sts get-caller-identity) to verify environment identity.", category="aws", permissions=["aws.read"])
def aws_identity(args, ctx):
    return ctx.backend("aws").identity()


@tool("aws_describe", "Call a read-only AWS API (describe-*/list-*/get-* ...). Mutating operations are refused.", category="aws", permissions=["aws.read"],
      input_schema={"type": "object", "properties": {"service": {"type": "string"}, "operation": {"type": "string"}, "params": {"type": "object"}},
                    "required": ["service", "operation"]}, timeout=90)
def aws_describe(args, ctx):
    _validate(args["service"], args["operation"])
    if not is_read_only(args["operation"]):
        raise ToolError(f"'{args['operation']}' is not a read-only operation; use aws_modify (requires approval)", kind="permission")
    return ctx.backend("aws").call(args["service"], args["operation"], dict(args.get("params") or {}))


@tool("aws_logs_filter", "Filter CloudWatch log events from a log group.", category="observability", permissions=["aws.read"],
      input_schema={"type": "object", "properties": {"log_group": {"type": "string"}, "pattern": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["log_group"]}, timeout=90)
def aws_logs_filter(args, ctx):
    params: dict[str, Any] = {"log_group_name": args["log_group"], "limit": int(args.get("limit") or 50)}
    if args.get("pattern"):
        params["filter_pattern"] = args["pattern"]
    data = ctx.backend("aws").call("logs", "filter-log-events", params)
    return {"events": [{"timestamp": e.get("timestamp"), "message": e.get("message")} for e in (data or {}).get("events", [])]}


class AwsModifyTool(Tool):
    spec = ToolSpec(name="aws_modify", description="Call a mutating AWS API (modify-*/update-*/create-*/put-*/start-/stop-...). Requires approval.",
                    permission=PermissionLevel.DEPLOY, risk_level=RiskLevel.HIGH, requires_approval=True, permissions=["aws.write"], category="aws",
                    mutating=True, timeout=180, rollback="apply the inverse API call with the previously described state (captured before the change)",
                    input_schema={"type": "object", "properties": {"service": {"type": "string"}, "operation": {"type": "string"}, "params": {"type": "object"}},
                                  "required": ["service", "operation"]})

    def run(self, args, ctx):
        _validate(args["service"], args["operation"])
        op = args["operation"]
        if op.startswith(("delete-", "terminate-", "deregister-", "remove-", "purge-")) or args["service"] == "iam":
            raise ToolError(f"'{args['service']} {op}' is a DESTROY-class operation; use aws_destroy", kind="permission")
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("aws").call(args["service"], op, dict(args.get("params") or {})), tool=self.name, args=args)


class AwsDestroyTool(Tool):
    spec = ToolSpec(name="aws_destroy", description="Call a destructive AWS API (delete-*/terminate-*, any IAM change). Explicit approval always required.",
                    permission=PermissionLevel.DESTROY, risk_level=RiskLevel.CRITICAL, requires_approval=True, permissions=["aws.destroy"], category="aws",
                    mutating=True, timeout=180, rollback="NOT AVAILABLE for most delete operations; restore from backup/IaC",
                    input_schema={"type": "object", "properties": {"service": {"type": "string"}, "operation": {"type": "string"}, "params": {"type": "object"}},
                                  "required": ["service", "operation"]})

    def run(self, args, ctx):
        _validate(args["service"], args["operation"])
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("aws").call(args["service"], args["operation"], dict(args.get("params") or {})), tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [aws_identity, aws_describe, aws_logs_filter, AwsModifyTool(), AwsDestroyTool()]
