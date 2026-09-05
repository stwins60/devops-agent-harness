"""Durable task state.

Each task lives in ``tasks/<TASK-ID>/`` with a machine readable ``task.json``
plus human readable markdown artifacts (plan.md, evidence.md, changes.md,
validation.md, final-report.md). The store is the single source of truth
for resuming interrupted work.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.audit.redaction import redact
from agent.models import (ApprovalRecord, Diagnosis, Environment, Evidence, Links, OperatingMode, Plan,
                          ProposedChange, TaskKind, TaskStage, TaskStatus, ValidationResult, new_id, now_iso)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str
    timestamp: str = field(default_factory=now_iso)
    agent: str = ""
    dry_run: bool = False
    rollback: Optional[str] = None
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "ok": self.ok, "summary": self.summary,
                "timestamp": self.timestamp, "agent": self.agent, "dry_run": self.dry_run,
                "rollback": self.rollback, "rolled_back": self.rolled_back}


@dataclass
class TaskState:
    id: str
    request: str
    kind: TaskKind = TaskKind.QUESTION
    mode: OperatingMode = OperatingMode.APPROVAL
    environment: Environment = Environment.UNKNOWN
    stage: TaskStage = TaskStage.RECEIVED
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    specialists: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None
    plan: Optional[Plan] = None
    changes: list[ProposedChange] = field(default_factory=list)
    validation: list[ValidationResult] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    links: Links = field(default_factory=Links)
    history: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    workspace: Optional[str] = None
    report: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    # -- mutation helpers -------------------------------------------------
    def transition(self, stage: TaskStage, status: TaskStatus = TaskStatus.RUNNING, detail: str = "") -> None:
        self.history.append({"from": self.stage.value, "to": stage.value, "status": status.value,
                             "at": now_iso(), "detail": detail})
        self.stage = stage
        self.status = status
        self.updated_at = now_iso()

    def add_evidence(self, ev: Evidence) -> Evidence:
        self.evidence.append(ev)
        return ev

    def note(self, text: str) -> None:
        self.notes.append(f"[{now_iso()}] {text}")

    def error(self, text: str) -> None:
        self.errors.append(f"[{now_iso()}] {text}")

    def facts(self) -> list[Evidence]:
        return [e for e in self.evidence if e.kind.value == "FACT"]

    @property
    def completed(self) -> bool:
        return self.status == TaskStatus.COMPLETED

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "request": self.request, "kind": self.kind.value, "mode": self.mode.value,
            "environment": self.environment.value, "stage": self.stage.value, "status": self.status.value,
            "created_at": self.created_at, "updated_at": self.updated_at, "specialists": self.specialists,
            "context": redact(self.context), "evidence": [e.to_dict() for e in self.evidence],
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "changes": [c.to_dict() for c in self.changes],
            "validation": [v.to_dict() for v in self.validation],
            "approvals": [a.to_dict() for a in self.approvals],
            "tool_calls": [redact(t.to_dict()) for t in self.tool_calls],
            "links": self.links.to_dict(), "history": self.history, "notes": self.notes, "errors": self.errors,
            "checkpoint": redact(self.checkpoint), "dry_run": self.dry_run, "workspace": self.workspace,
            "report": self.report, "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskState":
        st = cls(id=d["id"], request=d.get("request", ""), kind=TaskKind(d.get("kind", "question")),
                 mode=OperatingMode.parse(d.get("mode", "approval")), environment=Environment.parse(d.get("environment")),
                 stage=TaskStage(d.get("stage", "received")), status=TaskStatus(d.get("status", "pending")),
                 created_at=d.get("created_at", now_iso()), updated_at=d.get("updated_at", now_iso()),
                 specialists=list(d.get("specialists", [])), context=dict(d.get("context", {})),
                 evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])],
                 diagnosis=Diagnosis.from_dict(d["diagnosis"]) if d.get("diagnosis") else None,
                 plan=Plan.from_dict(d["plan"]) if d.get("plan") else None,
                 changes=[ProposedChange.from_dict(c) for c in d.get("changes", [])],
                 validation=[ValidationResult(**v) for v in d.get("validation", [])],
                 approvals=[ApprovalRecord(**a) for a in d.get("approvals", [])],
                 tool_calls=[ToolCallRecord(**t) for t in d.get("tool_calls", [])],
                 links=Links(**d.get("links", {})), history=list(d.get("history", [])), notes=list(d.get("notes", [])),
                 errors=list(d.get("errors", [])), checkpoint=dict(d.get("checkpoint", {})), dry_run=bool(d.get("dry_run", False)),
                 workspace=d.get("workspace"), report=d.get("report"), metrics=dict(d.get("metrics", {})))
        return st


class TaskStore:
    ARTIFACTS = ("plan.md", "evidence.md", "changes.md", "validation.md", "final-report.md", "incident-report.md")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalise_id(task_id: Optional[str], request: str = "") -> str:
        if task_id and _ID_RE.match(task_id):
            return task_id
        m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", request or "")
        if m:
            return m.group(1)
        return new_id("task")

    def dir(self, task_id: str) -> Path:
        if not _ID_RE.match(task_id):
            raise ValueError(f"invalid task id '{task_id}'")
        return self.root / task_id

    def exists(self, task_id: str) -> bool:
        return (self.root / task_id / "task.json").exists()

    def create(self, request: str, *, task_id: Optional[str] = None, kind: TaskKind = TaskKind.QUESTION,
               mode: OperatingMode = OperatingMode.APPROVAL, environment: Environment = Environment.UNKNOWN,
               dry_run: bool = False) -> TaskState:
        tid = self.normalise_id(task_id, request)
        if self.exists(tid):
            # keep prior runs for the same ticket: archive them
            prior = self.load(tid)
            if prior.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DENIED):
                self.archive(tid)
            else:
                # an in-progress task with the same id: resume instead of clobbering
                return prior
        state = TaskState(id=tid, request=request, kind=kind, mode=mode, environment=environment, dry_run=dry_run)
        self.save(state)
        return state

    def save(self, state: TaskState) -> Path:
        d = self.dir(state.id)
        d.mkdir(parents=True, exist_ok=True)
        state.updated_at = now_iso()
        payload = json.dumps(state.to_dict(), indent=2, default=str)
        tmp = d / "task.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        target = d / "task.json"
        for attempt in range(6):  # atomic replace; retried because sync clients (OneDrive, antivirus) briefly lock files on Windows
            try:
                tmp.replace(target)
                return d
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        target.write_text(payload, encoding="utf-8")  # last resort: direct write
        try:
            tmp.unlink()
        except OSError:
            pass
        return d

    def load(self, task_id: str) -> TaskState:
        path = self.dir(task_id) / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"task '{task_id}' not found in {self.root}")
        return TaskState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write_artifact(self, task_id: str, name: str, content: str) -> Path:
        d = self.dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        from agent.audit.redaction import redact_text
        path.write_text(redact_text(content), encoding="utf-8")
        return path

    def read_artifact(self, task_id: str, name: str) -> Optional[str]:
        path = self.dir(task_id) / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def list(self) -> list[TaskState]:
        out: list[TaskState] = []
        for child in sorted(self.root.iterdir()) if self.root.exists() else []:
            if (child / "task.json").exists():
                try:
                    out.append(self.load(child.name))
                except Exception:
                    continue
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    def archive(self, task_id: str) -> Optional[Path]:
        d = self.dir(task_id)
        if not d.exists():
            return None
        archive_root = self.root / "_archive"
        archive_root.mkdir(exist_ok=True)
        target = archive_root / f"{task_id}-{now_iso().replace(':', '').replace('-', '')}"
        shutil.move(str(d), str(target))
        return target

    def workspace_dir(self, task_id: str) -> Path:
        ws = self.dir(task_id) / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def resumable(self, task_id: str) -> tuple[bool, str]:
        st = self.load(task_id)
        if st.status == TaskStatus.COMPLETED:
            return False, "task already completed"
        if st.status == TaskStatus.DENIED:
            return False, "task was denied; start a new task to re-plan"
        return True, f"resume from stage '{st.stage.value}' (status {st.status.value})"
