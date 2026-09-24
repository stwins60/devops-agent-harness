"""LangFuse tracing backend.

Requires: ``pip install langfuse``
Config keys: base_url, public_key (or LANGFUSE_PUBLIC_KEY), secret_key (or LANGFUSE_SECRET_KEY), project.
"""
from __future__ import annotations

import os
from typing import Any, Optional


class LangfuseTracer:
    def __init__(self, *, base_url: str = "https://cloud.langfuse.com",
                 public_key: Optional[str] = None, secret_key: Optional[str] = None,
                 project: str = "devops-agent") -> None:
        try:
            from langfuse import Langfuse  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("Install langfuse: pip install langfuse") from exc

        self._project = project
        self._lf = Langfuse(
            public_key=public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=secret_key or os.environ.get("LANGFUSE_SECRET_KEY", ""),
            host=base_url,
        )

    def trace_llm(self, *, name: str, provider: str, model: str, prompt: str, completion: str,
                  prompt_tokens: int, completion_tokens: int, duration: float,
                  task_id: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        trace = self._lf.trace(name=name, session_id=task_id,
                               metadata={"project": self._project, **(metadata or {})},
                               tags=[f"provider:{provider}"])
        trace.generation(
            name=f"{provider}/{model}",
            model=model,
            input=prompt,
            output=completion,
            usage={"input": prompt_tokens, "output": completion_tokens},
            metadata={"duration_s": round(duration, 3)},
        )

    def trace_tool(self, *, name: str, tool: str, inputs: dict[str, Any], output: str,
                   success: bool, duration: float, task_id: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> None:
        trace = self._lf.trace(name=name, session_id=task_id,
                               metadata={"project": self._project, **(metadata or {})},
                               tags=[f"tool:{tool}"])
        trace.span(
            name=tool,
            input=inputs,
            output={"result": output, "success": success},
            metadata={"duration_s": round(duration, 3)},
            level="DEFAULT" if success else "ERROR",
        )

    def trace_task(self, *, task_id: str, request: str, status: str, stage: str,
                   duration: float, metadata: Optional[dict[str, Any]] = None) -> None:
        self._lf.trace(
            name="task",
            session_id=task_id,
            input={"request": request},
            output={"status": status, "stage": stage},
            metadata={"duration_s": round(duration, 3), "project": self._project, **(metadata or {})},
        )

    def flush(self) -> None:
        self._lf.flush()
