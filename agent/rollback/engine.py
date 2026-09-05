"""Rollback engine.

Every mutating tool call records a rollback entry (from the tool's declared
rollback template plus any concrete undo data captured by the tool). The
engine can replay entries in reverse order and reports explicitly when a
change cannot be rolled back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from agent.models import ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from agent.executor import ToolExecutor
    from agent.state.store import TaskState


@dataclass
class RollbackEntry:
    tool: str
    args: dict[str, Any]
    description: Optional[str]
    result: Optional[dict[str, Any]] = None
    possible: bool = True
    executed: bool = False
    ok: Optional[bool] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "description": self.description, "possible": self.possible,
                "executed": self.executed, "ok": self.ok, "detail": self.detail, "result": self.result}


@dataclass
class RollbackPlan:
    entries: list[RollbackEntry] = field(default_factory=list)

    def add(self, entry: RollbackEntry) -> None:
        self.entries.append(entry)

    def render(self) -> str:
        if not self.entries:
            return "No mutating operations were performed; nothing to roll back."
        lines = []
        for i, e in enumerate(reversed(self.entries), 1):
            status = "" if not e.executed else (" [rolled back]" if e.ok else " [ROLLBACK FAILED]")
            if e.possible:
                lines.append(f"{i}. {e.tool}: {e.description or 'undo via tool rollback'}{status}")
            else:
                lines.append(f"{i}. {e.tool}: ROLLBACK NOT POSSIBLE - {e.detail or 'no automatic rollback available; manual intervention required'}")
        return "\n".join(lines)

    def to_dict(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]

    @classmethod
    def from_dict(cls, data: list[dict[str, Any]]) -> "RollbackPlan":
        plan = cls()
        for d in data or []:
            plan.add(RollbackEntry(tool=d["tool"], args=d.get("args", {}), description=d.get("description"), result=d.get("result"),
                                   possible=d.get("possible", True), executed=d.get("executed", False), ok=d.get("ok"),
                                   detail=d.get("detail", "")))
        return plan


class RollbackEngine:
    def __init__(self, executor: "ToolExecutor") -> None:
        self.executor = executor

    def execute(self, task: "TaskState", plan: RollbackPlan, *, stop_on_failure: bool = False) -> list[RollbackEntry]:
        """Roll back all executed mutations in reverse order. Returns the entries processed."""
        processed: list[RollbackEntry] = []
        for entry in reversed(plan.entries):
            if entry.executed:
                continue
            if not entry.possible:
                entry.executed = True
                entry.ok = False
                entry.detail = entry.detail or "no automatic rollback available"
                self.executor.audit.rollback(task=task.id, tool=entry.tool, ok=False, detail=entry.detail)
                processed.append(entry)
                if stop_on_failure:
                    break
                continue
            result = self.executor.rollback(entry.tool, entry.args, entry.result, task)
            entry.executed = True
            entry.ok = bool(result and result.ok)
            entry.detail = (result.error if result and not result.ok else (result.text[:300] if result else "no rollback handler"))
            processed.append(entry)
            if not entry.ok and stop_on_failure:
                break
        task.checkpoint["rollback"] = plan.to_dict()
        return processed
