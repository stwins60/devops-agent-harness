"""Structured audit logging and harness metrics.

Every tool invocation, approval decision and stage transition is appended as
one JSON object per line. All payloads pass through redaction first.
"""
from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from agent.audit.redaction import redact
from agent.models import now_iso


class Metrics:
    """In-memory counters for harness observability (exported into the audit log on flush)."""

    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.tokens: Counter[str] = Counter()
        self._lock = threading.Lock()

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def timing(self, name: str, seconds: float) -> None:
        with self._lock:
            self.timings[name].append(seconds)

    def add_tokens(self, provider: str, prompt: int = 0, completion: int = 0) -> None:
        with self._lock:
            self.tokens[f"{provider}.prompt"] += prompt
            self.tokens[f"{provider}.completion"] += completion

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calls = self.counters.get("tool.calls", 0)
            failures = self.counters.get("tool.failures", 0)
            approvals = self.counters.get("approval.requested", 0)
            approved = self.counters.get("approval.approved", 0)
            return {
                "counters": dict(self.counters),
                "tokens": dict(self.tokens),
                "timings": {k: {"count": len(v), "total": round(sum(v), 3), "max": round(max(v), 3)}
                            for k, v in self.timings.items() if v},
                "derived": {
                    "tool_success_rate": round((calls - failures) / calls, 3) if calls else None,
                    "approval_rate": round(approved / approvals, 3) if approvals else None,
                },
            }


class AuditLogger:
    def __init__(self, path: Optional[Path], metrics: Optional[Metrics] = None, echo: bool = False) -> None:
        self.path = Path(path) if path else None
        self.metrics = metrics or Metrics()
        self.echo = echo
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []  # in-memory tail for tests / reports
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"timestamp": now_iso(), "event": event}
        record.update(redact(fields))
        line = json.dumps(record, default=str, sort_keys=False)
        with self._lock:
            self.records.append(record)
            if len(self.records) > 5000:
                self.records = self.records[-2500:]
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        if self.echo:
            print(f"[audit] {line}")
        return record

    def tool_call(self, *, agent: str, task: Optional[str], tool: str, arguments: dict[str, Any], risk: str,
                  permission: str, approval: bool, result: str, duration: float = 0.0, error: Optional[str] = None,
                  environment: str = "unknown", dry_run: bool = False) -> dict[str, Any]:
        self.metrics.incr("tool.calls")
        if result not in ("success", "dry-run", "skipped"):
            self.metrics.incr("tool.failures")
        self.metrics.timing(f"tool.{tool}", duration)
        return self.log("tool_call", agent=agent, task=task, tool=tool, arguments=arguments, risk=risk,
                        permission=permission, approval=approval, result=result, duration=round(duration, 3),
                        error=error, environment=environment, dry_run=dry_run)

    def approval(self, *, task: Optional[str], operation: str, decision: str, decided_by: str, risk: str,
                 environment: str) -> dict[str, Any]:
        self.metrics.incr("approval.requested")
        if decision == "approve":
            self.metrics.incr("approval.approved")
        elif decision == "deny":
            self.metrics.incr("approval.denied")
        return self.log("approval", task=task, operation=operation, decision=decision, decided_by=decided_by,
                        risk=risk, environment=environment)

    def stage(self, task: str, stage: str, status: str, detail: str = "") -> dict[str, Any]:
        return self.log("stage", task=task, stage=stage, status=status, detail=detail)

    def policy_block(self, *, task: Optional[str], tool: str, reason: str, environment: str) -> dict[str, Any]:
        self.metrics.incr("policy.blocked")
        return self.log("policy_block", task=task, tool=tool, reason=reason, environment=environment)

    def rollback(self, *, task: str, tool: str, ok: bool, detail: str = "") -> dict[str, Any]:
        self.metrics.incr("rollback.executed")
        if not ok:
            self.metrics.incr("rollback.failed")
        return self.log("rollback", task=task, tool=tool, ok=ok, detail=detail)

    def model_usage(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int, duration: float) -> None:
        self.metrics.add_tokens(provider, prompt_tokens, completion_tokens)
        self.metrics.incr("model.calls")
        self.metrics.timing("model.call", duration)
        self.log("model_usage", provider=provider, model=model, prompt_tokens=prompt_tokens,
                 completion_tokens=completion_tokens, duration=round(duration, 3))

    def flush_metrics(self, task: Optional[str] = None) -> dict[str, Any]:
        snap = self.metrics.snapshot()
        self.log("metrics", task=task, **snap)
        return snap
