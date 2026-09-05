"""Specialist agent base class.

A specialist encapsulates domain knowledge as *code*: a structured
investigation workflow, evidence analyzers, change proposals, implementation
and validation. Specialists never execute tools directly; every call goes
through the executor so policy, approval and audit apply uniformly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from agent.models import Diagnosis, Plan, ProposedChange, TaskKind, ToolResult, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.runbooks.loader import Runbook
from agent.state.store import TaskState

if TYPE_CHECKING:  # pragma: no cover
    from agent.harness import Harness

Analyzer = Callable[[EvidenceLog], list]


@dataclass
class Investigation:
    """Shared working state for one task across all specialists involved."""

    task: TaskState
    harness: "Harness"
    log: EvidenceLog = field(default_factory=EvidenceLog)
    targets: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    failures: list[ToolResult] = field(default_factory=list)
    runbooks_used: list[str] = field(default_factory=list)
    blocked: Optional[str] = None  # set when a human must act before continuing

    def target(self, key: str, default: Any = None) -> Any:
        return self.targets.get(key, default)

    def set_target(self, key: str, value: Any) -> None:
        if value not in (None, ""):
            self.targets.setdefault(key, value)


class Specialist:
    name = "base"
    description = ""
    domains: list[str] = []
    keywords: list[str] = []

    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    # -- routing ----------------------------------------------------------
    def score(self, request: str, kind: TaskKind, targets: dict[str, Any]) -> float:
        low = request.lower()
        hits = 0.0
        for kw in self.keywords:
            if re.search(rf"\b{re.escape(kw)}\b", low):
                hits += 1.0
        for d in self.domains:
            if d in targets.get("domains", []):
                hits += 2.0
        return hits

    # -- lifecycle hooks --------------------------------------------------
    def investigate(self, inv: Investigation) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def analyzers(self) -> list[tuple[str, Analyzer]]:
        return []

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        return None

    def implement(self, inv: Investigation, plan: Plan) -> None:
        """Default implementation: execute every proposed change that names a tool."""
        for change in plan.changes:
            if change.applied or not change.tool:
                continue
            result = self.call(inv, change.tool, change.args, purpose=change.description, expected_impact=change.description)
            change.result = {"ok": result.ok, "error": result.error, "dry_run": result.dry_run,
                             "summary": (result.text if result.ok else (result.error or ""))[:500]}
            change.applied = bool(result.ok and not result.dry_run)
            if isinstance(result.output, dict) and result.output.get("diff"):
                change.diff = result.output["diff"]
            if not result.ok:
                inv.task.error(f"change failed: {change.description}: {result.error}")
                if result.failure_kind in ("denied", "policy", "skipped"):
                    inv.blocked = f"{change.description}: {result.error}"
                    break
                break
        inv.task.changes = plan.changes

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        return []

    # -- helpers ----------------------------------------------------------
    def call(self, inv: Investigation, tool: str, args: Optional[dict[str, Any]] = None, *, purpose: str = "", **kw: Any) -> ToolResult:
        result = self.h.executor.run(tool, args or {}, inv.task, agent=self.name, purpose=purpose, **kw)
        if not result.ok and not result.dry_run:
            inv.failures.append(result)
            if result.failure_kind not in ("not_found", "invalid"):
                inv.log.fact(f"{tool} failed ({result.failure_kind}): {(result.error or '')[:200]}", source=f"{tool}({_short(args)})",
                             tool_failure=result.failure_kind, tool=tool)
            if result.failure_kind in ("auth", "permission", "network", "unavailable", "policy", "denied", "loop_guard"):
                inv.notes.append(f"{tool}: {result.advice or result.error}")
        return result

    def use_runbook(self, inv: Investigation, text: str, domain: Optional[str] = None) -> Optional[Runbook]:
        matches = self.h.runbooks.find(text, domain=domain, limit=1)
        if matches:
            rb = matches[0]
            if rb.name not in inv.runbooks_used:
                inv.runbooks_used.append(rb.name)
                inv.log.inference(f"Using runbook '{rb.name}' ({rb.domain}) as the investigation procedure.", source="runbooks", runbook=rb.name, confidence=1.0)
            return rb
        return None

    @staticmethod
    def file_change(description: str, path: str, old: str, new: str, *, rollback: Optional[str] = None) -> ProposedChange:
        from agent.models import PermissionLevel, RiskLevel

        return ProposedChange(description=description, kind="file", target=path, tool="fs_replace", args={"path": path, "old": old, "new": new},
                              risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY, rollback=rollback or f"restore previous content of {path} (git checkout -- {path})")


def _short(args: Optional[dict[str, Any]]) -> str:
    if not args:
        return ""
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items() if k not in ("content", "manifest", "body"))
