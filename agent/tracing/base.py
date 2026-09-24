"""Base Tracer protocol and Span dataclass used by all backends."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class Span:
    name: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_span_id: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    error: Optional[str] = None
    level: str = "DEFAULT"  # DEFAULT | WARNING | ERROR

    def finish(self, *, outputs: Optional[dict[str, Any]] = None, error: Optional[str] = None) -> "Span":
        self.end_time = time.time()
        if outputs:
            self.outputs.update(outputs)
        if error:
            self.error = error
            self.level = "ERROR"
        return self

    @property
    def duration(self) -> float:
        return (self.end_time or time.time()) - self.start_time


@runtime_checkable
class Tracer(Protocol):
    """Minimal tracing interface all backends must implement."""

    def trace_llm(
        self,
        *,
        name: str,
        provider: str,
        model: str,
        prompt: str,
        completion: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration: float,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def trace_tool(
        self,
        *,
        name: str,
        tool: str,
        inputs: dict[str, Any],
        output: str,
        success: bool,
        duration: float,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def trace_task(
        self,
        *,
        task_id: str,
        request: str,
        status: str,
        stage: str,
        duration: float,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def flush(self) -> None: ...


class NullTracer:
    """No-op tracer. Used when tracing is disabled (default)."""

    def trace_llm(self, **_: Any) -> None:
        pass

    def trace_tool(self, **_: Any) -> None:
        pass

    def trace_task(self, **_: Any) -> None:
        pass

    def flush(self) -> None:
        pass
