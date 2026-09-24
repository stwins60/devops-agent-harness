"""MultiTracer: fan-out tracer that forwards to N backends simultaneously."""
from __future__ import annotations

from typing import Any, Optional

from agent.tracing.base import Tracer


class MultiTracer:
    """Forwards every trace event to all configured backends.

    Example config::

        tracing:
          backends:
            - backend: langfuse
              base_url: http://langfuse.internal:3000
              public_key: pk-lf-...
              secret_key: sk-lf-...
              project: devops-agent
            - backend: langsmith
              api_key: ls__...
              project: devops-agent
    """

    def __init__(self, tracers: list[Tracer]) -> None:
        self._tracers = tracers

    def trace_llm(self, **kwargs: Any) -> None:
        for t in self._tracers:
            try:
                t.trace_llm(**kwargs)
            except Exception:  # noqa: BLE001
                pass

    def trace_tool(self, **kwargs: Any) -> None:
        for t in self._tracers:
            try:
                t.trace_tool(**kwargs)
            except Exception:  # noqa: BLE001
                pass

    def trace_task(self, **kwargs: Any) -> None:
        for t in self._tracers:
            try:
                t.trace_task(**kwargs)
            except Exception:  # noqa: BLE001
                pass

    def flush(self) -> None:
        for t in self._tracers:
            try:
                t.flush()
            except Exception:  # noqa: BLE001
                pass
