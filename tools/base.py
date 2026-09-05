"""Tool abstraction.

A tool is a named, schema-described capability with a declared permission
level and risk. Tools never talk to the policy engine themselves: the
executor (agent/executor.py) wraps every call with policy evaluation,
approval, audit logging and rollback bookkeeping.

Backends
--------
Most tools delegate to a *backend* implementing a small Protocol (e.g.
``KubernetesBackend``). Each backend has a real implementation (CLI / REST /
SDK) and a mock implementation used in ``--mock`` mode and in tests.
"""
from __future__ import annotations

import abc
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent.models import Environment, PermissionLevel, RiskLevel, ToolResult, ToolSpec


class ToolError(Exception):
    """Raised by tools for expected failures. ``kind`` categorises the failure for recovery logic."""

    def __init__(self, message: str, kind: str = "unknown", advice: Optional[str] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.advice = advice


@dataclass
class ToolContext:
    """Runtime context injected into every tool invocation."""

    environment: Environment = Environment.UNKNOWN
    task_id: Optional[str] = None
    agent: str = "orchestrator"
    workspace: Optional[Path] = None
    project_root: Optional[Path] = None
    dry_run: bool = False
    mock: bool = False
    backends: dict[str, Any] = field(default_factory=dict)
    config: Any = None
    timeout: Optional[int] = None

    def backend(self, name: str) -> Any:
        try:
            return self.backends[name]
        except KeyError as exc:
            raise ToolError(f"backend '{name}' is not configured", kind="unavailable",
                            advice=f"configure the {name} integration or run with --mock") from exc


class Tool(abc.ABC):
    """Base class for all tools."""

    spec: ToolSpec

    def __init__(self, spec: Optional[ToolSpec] = None) -> None:
        if spec is not None:
            self.spec = spec
        if not getattr(self, "spec", None):
            raise ValueError(f"{type(self).__name__} has no ToolSpec")

    @property
    def name(self) -> str:
        return self.spec.name

    def validate_args(self, args: dict[str, Any]) -> list[str]:
        """Very small JSON-schema subset validation: required keys + basic types."""
        errors: list[str] = []
        schema = self.spec.input_schema or {}
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in args or args[key] in (None, ""):
                errors.append(f"missing required argument '{key}'")
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
        for key, value in args.items():
            if key in props and "type" in props[key] and value is not None:
                expected = type_map.get(props[key]["type"])
                if expected and not isinstance(value, expected):
                    errors.append(f"argument '{key}' must be {props[key]['type']}")
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in props:
                    errors.append(f"unexpected argument '{key}'")
        return errors

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute the tool. Implementations should raise ToolError for expected failures."""

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> Optional[ToolResult]:
        """Undo the effect of a previous successful run, if possible. Default: not supported."""
        return None

    def describe_rollback(self, args: dict[str, Any]) -> Optional[str]:
        if not self.spec.rollback:
            return None
        try:
            return self.spec.rollback.format(**{k: v for k, v in args.items() if isinstance(v, (str, int, float))})
        except (KeyError, IndexError):
            return self.spec.rollback

    def dry_run_result(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output={"dry_run": True, "tool": self.name, "args": args,
                                           "message": f"dry-run: {self.name} would execute with {args}"},
                          tool=self.name, args=args, dry_run=True)


class FunctionTool(Tool):
    """Wrap a plain callable ``fn(args, ctx) -> Any`` as a tool."""

    def __init__(self, spec: ToolSpec, fn: Callable[[dict[str, Any], ToolContext], Any],
                 rollback_fn: Optional[Callable[[dict[str, Any], ToolResult, ToolContext], Any]] = None) -> None:
        super().__init__(spec)
        self.fn = fn
        self.rollback_fn = rollback_fn

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.time()
        output = self.fn(args, ctx)
        if isinstance(output, ToolResult):
            output.tool = output.tool or self.name
            output.duration = output.duration or (time.time() - started)
            return output
        return ToolResult(ok=True, output=output, duration=time.time() - started, tool=self.name, args=args)

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> Optional[ToolResult]:
        if not self.rollback_fn:
            return None
        out = self.rollback_fn(args, result, ctx)
        if isinstance(out, ToolResult):
            return out
        return ToolResult(ok=True, output=out, tool=f"{self.name}.rollback", args=args)


def tool(name: str, description: str, *, permission: PermissionLevel = PermissionLevel.READ,
         risk: RiskLevel = RiskLevel.LOW, requires_approval: bool = False, category: str = "general",
         timeout: int = 60, rollback: Optional[str] = None, input_schema: Optional[dict[str, Any]] = None,
         output_schema: Optional[dict[str, Any]] = None, permissions: Optional[list[str]] = None,
         mutating: Optional[bool] = None, tags: Optional[list[str]] = None) -> Callable[[Callable[..., Any]], FunctionTool]:
    """Decorator turning a function into a registered-ready FunctionTool."""

    def wrap(fn: Callable[..., Any]) -> FunctionTool:
        spec = ToolSpec(
            name=name, description=inspect.cleandoc(description), risk_level=risk, requires_approval=requires_approval,
            permission=permission, permissions=list(permissions or []), input_schema=input_schema or {},
            output_schema=output_schema or {}, timeout=timeout, rollback=rollback, category=category,
            mutating=(permission >= PermissionLevel.MODIFY) if mutating is None else mutating, tags=list(tags or []),
        )
        return FunctionTool(spec, fn)

    return wrap


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (failure_kind, advice) for the error-recovery layer."""
    if isinstance(exc, ToolError):
        return exc.kind, exc.advice or ""
    msg = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in msg or "timeout" in msg:
        return "timeout", "the command exceeded its timeout; narrow the query or raise the tool timeout"
    if any(k in msg for k in ("accessdenied", "access denied", "forbidden", "403", "unauthorizedoperation", "permission denied", "not authorized")):
        return "permission", "missing permission: do not retry; identify the missing IAM/RBAC permission or use an alternative read-only API"
    if any(k in msg for k in ("expiredtoken", "token has expired", "invalid credentials", "401", "unauthorized", "authentication", "expired")):
        return "auth", "authentication failed or credentials expired: refresh credentials before retrying"
    if any(k in msg for k in ("rate limit", "throttl", "429", "too many requests")):
        return "rate_limit", "rate limited: back off exponentially and reduce call volume"
    if any(k in msg for k in ("connection refused", "no such host", "name or service not known", "unreachable", "network is unreachable", "dial tcp", "could not resolve", "connection reset", "eof", "temporary failure")):
        return "network", "network failure: verify connectivity/VPN/cluster endpoint before retrying"
    if any(k in msg for k in ("not found", "notfound", "404", "does not exist", "no such")):
        return "not_found", "the resource does not exist: verify names, namespace, region or account"
    if any(k in msg for k in ("invalid", "malformed", "syntax", "validation", "parse error")):
        return "invalid", "invalid input or configuration: fix the input and re-run"
    return "unknown", ""
