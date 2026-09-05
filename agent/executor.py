"""Tool executor: the single choke point through which every tool call passes.

    specialist / model  ->  executor.run()  ->  policy  ->  approval  ->  tool  ->  audit + state + rollback

Nothing in the harness invokes ``tool.run`` directly except this class.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from agent.approvals.engine import ApprovalEngine
from agent.audit.logger import AuditLogger
from agent.audit.redaction import redact
from agent.config import HarnessConfig
from agent.models import ApprovalDecision, ApprovalRequest, PermissionLevel, RiskLevel, ToolResult
from agent.policies.engine import PolicyEngine
from agent.reports.render import render_plan
from agent.rollback.engine import RollbackEntry, RollbackPlan
from agent.state.store import TaskState, TaskStore, ToolCallRecord
from tools.adapters import McpTool
from tools.base import ToolContext, ToolError, classify_exception
from tools.registry import ToolRegistry


class LoopGuardError(Exception):
    pass


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, approvals: ApprovalEngine, audit: AuditLogger, config: HarnessConfig,
                 backends: dict[str, Any], store: Optional[TaskStore] = None) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.config = config
        self.backends = backends
        self.store = store
        self._call_counter: dict[str, Counter[str]] = {}

    # ------------------------------------------------------------------
    def context_for(self, task: TaskState, timeout: Optional[int] = None) -> ToolContext:
        return ToolContext(environment=task.environment, task_id=task.id, workspace=Path(task.workspace) if task.workspace else None,
                           project_root=self.config.project_root, dry_run=task.dry_run, mock=self.config.mock, backends=self.backends,
                           config=self.config, timeout=timeout)

    def _guard(self, task: TaskState, tool_name: str, args: dict[str, Any]) -> None:
        limits = self.config.limits
        if len(task.tool_calls) >= limits.max_tool_calls:
            raise LoopGuardError(f"loop guard: task exceeded {limits.max_tool_calls} tool calls")
        key = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
        counter = self._call_counter.setdefault(task.id, Counter())
        counter[key] += 1
        if counter[key] > limits.max_repeated_calls:
            raise LoopGuardError(f"loop guard: '{tool_name}' called {counter[key]} times with identical arguments")

    # ------------------------------------------------------------------
    def run(self, tool_name: str, args: Optional[dict[str, Any]], task: TaskState, *, agent: str = "orchestrator", purpose: str = "",
            expected_impact: str = "", resources: Optional[list[str]] = None, cost_note: Optional[str] = None) -> ToolResult:
        args = dict(args or {})
        started = time.time()
        env = task.environment
        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error=f"unknown tool '{tool_name}'", failure_kind="invalid", tool=tool_name, args=args),
                                risk="n/a", permission="n/a", approval=False, started=started)
        errors = tool.validate_args(args)
        if errors:
            return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error="; ".join(errors), failure_kind="invalid", tool=tool_name, args=args),
                                risk=tool.spec.risk_level.value, permission=tool.spec.permission.name, approval=False, started=started)
        try:
            self._guard(task, tool_name, args)
        except LoopGuardError as exc:
            task.error(str(exc))
            return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error=str(exc), failure_kind="loop_guard", tool=tool_name, args=args),
                                risk=tool.spec.risk_level.value, permission=tool.spec.permission.name, approval=False, started=started)

        command = str(args.get("command")) if tool_name == "shell_run" else None
        target_branch = str(args.get("branch")) if tool_name.startswith("git_push") else None
        decision = self.policy.evaluate(tool.spec, args, environment=env, mode=task.mode, command=command, target_branch=target_branch)
        if not decision.allowed:
            self.audit.policy_block(task=task.id, tool=tool_name, reason=decision.reason, environment=env.value)
            task.note(f"policy blocked {tool_name}: {decision.reason}")
            return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error=f"blocked by policy: {decision.reason}", failure_kind="policy",
                                                                          tool=tool_name, args=args, advice="choose a read-only alternative or change operating mode/environment"),
                                risk=decision.risk.value, permission=decision.permission.name, approval=False, started=started)

        approved = False
        if decision.requires_approval and not task.dry_run:
            req = ApprovalRequest(
                operation=f"{tool_name} {command or _summarise_args(args)}".strip(), description=purpose or tool.spec.description,
                environment=env.value, risk=decision.risk, resources=resources or _resources_from_args(args),
                expected_impact=expected_impact or _default_impact(tool.spec.permission), rollback=tool.describe_rollback(args) or "",
                diff=args.get("diff") or (args.get("manifest") if isinstance(args.get("manifest"), str) else None),
                plan=render_plan(task.plan) if task.plan else None, tool=tool_name, args=redact(args), cost_note=cost_note,
            )
            task.status = task.status.__class__("waiting_approval")
            self._save(task)
            outcome = self.approvals.ask(req, explicit=decision.explicit_confirmation)
            task.approvals.append(outcome.record(req))
            self.audit.approval(task=task.id, operation=req.operation, decision=outcome.decision.value, decided_by=outcome.decided_by,
                                risk=decision.risk.value, environment=env.value)
            task.status = task.status.__class__("running")
            if outcome.decision == ApprovalDecision.DENY:
                task.note(f"approval denied for {tool_name}: {outcome.note}")
                return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error=f"approval denied: {outcome.note}", failure_kind="denied", tool=tool_name, args=args),
                                    risk=decision.risk.value, permission=decision.permission.name, approval=False, started=started, status="denied")
            if outcome.decision == ApprovalDecision.SKIP:
                task.note(f"step skipped by approver: {tool_name}")
                return self._finish(task, agent, tool_name, args, ToolResult(ok=False, error="skipped by approver", failure_kind="skipped", skipped=True, tool=tool_name, args=args),
                                    risk=decision.risk.value, permission=decision.permission.name, approval=False, started=started, status="skipped")
            approved = True
        elif decision.requires_approval and task.dry_run:
            task.note(f"dry-run: {tool_name} would require approval ({decision.reason})")

        ctx = self.context_for(task)
        result = self._execute(tool, args, ctx, task)
        rollback_desc = tool.describe_rollback(args) if (tool.spec.mutating and result.ok and not result.dry_run) else None
        if tool.spec.mutating and result.ok and not result.dry_run:
            has_handler = type(tool).rollback is not _Tool.rollback
            self._record_rollback(task, tool_name, args, result, rollback_desc, possible=has_handler or bool(rollback_desc))
        return self._finish(task, agent, tool_name, args, result, risk=decision.risk.value, permission=decision.permission.name, approval=approved,
                            started=started, rollback=rollback_desc)

    def _execute(self, tool, args: dict[str, Any], ctx: ToolContext, task: TaskState) -> ToolResult:
        if ctx.dry_run and tool.spec.mutating and isinstance(tool, McpTool):
            return tool.dry_run_result(args, ctx)
        timeout = ctx.timeout or tool.spec.timeout or self.config.limits.default_timeout
        attempts = 0
        while True:
            attempts += 1
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(tool.run, args, ctx)
                    result = future.result(timeout=timeout + 5)
            except concurrent.futures.TimeoutError:
                return ToolResult(ok=False, error=f"tool '{tool.name}' timed out after {timeout}s", failure_kind="timeout", tool=tool.name, args=args,
                                  advice="narrow the request or raise the timeout; the underlying process may still be running")
            except ToolError as exc:
                result = ToolResult(ok=False, error=str(exc), failure_kind=exc.kind, advice=exc.advice, tool=tool.name, args=args)
            except Exception as exc:  # noqa: BLE001 - convert everything into a structured failure
                kind, advice = classify_exception(exc)
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", failure_kind=kind, advice=advice, tool=tool.name, args=args)
            if result.ok or result.failure_kind not in ("network", "rate_limit") or tool.spec.mutating or attempts > self.config.limits.max_retries_transient:
                if not result.ok and not result.advice:
                    result.advice = _advice(result.failure_kind)
                return result
            time.sleep(0.2 * attempts)

    def _record_rollback(self, task: TaskState, tool_name: str, args: dict[str, Any], result: ToolResult, description: Optional[str], possible: bool) -> None:
        plan = self.rollback_plan(task)
        plan.add(RollbackEntry(tool=tool_name, args=redact(args), description=description, result=redact(result.output) if isinstance(result.output, dict) else None,
                               possible=possible, detail="" if possible else "tool declares no rollback"))
        task.checkpoint["rollback"] = plan.to_dict()

    def rollback_plan(self, task: TaskState) -> RollbackPlan:
        return RollbackPlan.from_dict(task.checkpoint.get("rollback", []))

    def _finish(self, task: TaskState, agent: str, tool_name: str, args: dict[str, Any], result: ToolResult, *, risk: str, permission: str,
                approval: bool, started: float, status: Optional[str] = None, rollback: Optional[str] = None) -> ToolResult:
        result.tool = result.tool or tool_name
        result.duration = result.duration or (time.time() - started)
        status = status or ("dry-run" if result.dry_run else "success" if result.ok else "skipped" if result.skipped else f"error:{result.failure_kind or 'unknown'}")
        self.audit.tool_call(agent=agent, task=task.id, tool=tool_name, arguments=args, risk=risk, permission=permission, approval=approval,
                             result=status, duration=result.duration, error=result.error, environment=task.environment.value, dry_run=result.dry_run)
        summary = (result.error or result.text)[:400]
        task.tool_calls.append(ToolCallRecord(tool=tool_name, args=redact(args), ok=result.ok, summary=summary, agent=agent, dry_run=result.dry_run, rollback=rollback))
        self._save(task)
        return result

    def _save(self, task: TaskState) -> None:
        if self.store:
            try:
                self.store.save(task)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def rollback(self, tool_name: str, args: dict[str, Any], result_output: Optional[dict[str, Any]], task: TaskState) -> Optional[ToolResult]:
        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            return ToolResult(ok=False, error=f"unknown tool '{tool_name}'", tool=tool_name)
        ctx = self.context_for(task)
        try:
            out = tool.rollback(args, ToolResult(ok=True, output=result_output, tool=tool_name, args=args), ctx)
        except ToolError as exc:
            out = ToolResult(ok=False, error=str(exc), failure_kind=exc.kind, tool=f"{tool_name}.rollback", args=args)
        except Exception as exc:  # noqa: BLE001
            out = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", tool=f"{tool_name}.rollback", args=args)
        if out is None:
            out = ToolResult(ok=False, error="tool provides no automatic rollback", failure_kind="unavailable", tool=f"{tool_name}.rollback", args=args)
        self.audit.rollback(task=task.id, tool=tool_name, ok=out.ok, detail=(out.error or "")[:300])
        for rec in task.tool_calls:
            if rec.tool == tool_name and rec.args == redact(args) and not rec.rolled_back:
                rec.rolled_back = out.ok
                break
        self._save(task)
        return out

    def rollback_all(self, task: TaskState) -> list[RollbackEntry]:
        from agent.rollback.engine import RollbackEngine

        plan = self.rollback_plan(task)
        entries = RollbackEngine(self).execute(task, plan)
        task.checkpoint["rollback"] = plan.to_dict()
        self._save(task)
        return entries


from tools.base import Tool as _Tool  # noqa: E402


def _summarise_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        if k in ("content", "manifest", "body", "diff", "description"):
            parts.append(f"{k}=<{len(str(v))} chars>")
        else:
            s = str(v)
            parts.append(f"{k}={s[:60]}{'...' if len(s) > 60 else ''}")
    return " ".join(parts)


def _resources_from_args(args: dict[str, Any]) -> list[str]:
    out = []
    for k in ("kind", "name", "namespace", "path", "branch", "unit", "container", "service", "key", "target", "head", "playbook", "dir"):
        if args.get(k):
            out.append(f"{k}: {args[k]}")
    return out


def _default_impact(permission: PermissionLevel) -> str:
    return {PermissionLevel.MODIFY: "local or repository modification", PermissionLevel.DEPLOY: "running system changes (deploy/restart/apply)",
            PermissionLevel.DESTROY: "IRREVERSIBLE destruction of resources or data"}.get(permission, "none")


def _advice(kind: Optional[str]) -> str:
    return {
        "auth": "credentials rejected or expired: refresh them before retrying; do not loop",
        "permission": "missing permission: identify the required role/scope or use an alternative read-only API; do not retry",
        "network": "endpoint unreachable: verify connectivity (VPN, DNS, cluster endpoint) before retrying",
        "rate_limit": "rate limited: reduce call volume and back off",
        "timeout": "operation timed out: narrow the query or raise the timeout",
        "not_found": "verify identifiers (names, namespace, region, project key)",
        "invalid": "fix the input or configuration and re-run",
        "unavailable": "the integration/binary is not available in this environment; configure it or run with --mock",
        "policy": "blocked by policy: read-only alternative or environment/mode change required",
        "denied": "approval denied: the operation was not performed",
        "loop_guard": "the harness stopped a repetitive loop; change strategy or escalate to a human",
    }.get(kind or "", "unexpected failure: inspect the error and decide whether a human is needed")
