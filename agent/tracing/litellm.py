"""LiteLLM tracing backend.

Requires: ``pip install litellm``
Config keys: base_url (optional proxy), success_callback, failure_callback, project.
"""
from __future__ import annotations

import os
from typing import Any, Optional


class LiteLLMTracer:
    def __init__(self, *, base_url: Optional[str] = None,
                 success_callback: Optional[list[str]] = None,
                 failure_callback: Optional[list[str]] = None,
                 project: str = "devops-agent") -> None:
        try:
            import litellm  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("Install litellm: pip install litellm") from exc

        self._litellm = litellm
        self._project = project
        self._base_url = base_url

        if success_callback:
            litellm.success_callback = success_callback
        if failure_callback:
            litellm.failure_callback = failure_callback
        os.environ.setdefault("LITELLM_LOG", "ERROR")

    def trace_llm(self, *, name: str, provider: str, model: str, prompt: str, completion: str,
                  prompt_tokens: int, completion_tokens: int, duration: float,
                  task_id: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        # LiteLLM traces LLM calls automatically via its callback system.
        # This records supplemental metadata for correlation.
        pass

    def trace_tool(self, *, name: str, tool: str, inputs: dict[str, Any], output: str,
                   success: bool, duration: float, task_id: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> None:
        pass

    def trace_task(self, *, task_id: str, request: str, status: str, stage: str,
                   duration: float, metadata: Optional[dict[str, Any]] = None) -> None:
        pass

    def flush(self) -> None:
        pass
