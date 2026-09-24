"""LangSmith tracing backend.

Requires: ``pip install langsmith``
Config keys: base_url, api_key (or LANGCHAIN_API_KEY), project.
"""
from __future__ import annotations

import os
from typing import Any, Optional


class LangSmithTracer:
    def __init__(self, *, base_url: str = "https://api.smith.langchain.com",
                 api_key: Optional[str] = None, project: str = "devops-agent") -> None:
        try:
            from langsmith import Client  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("Install langsmith: pip install langsmith") from exc

        self._project = project
        self._client = Client(
            api_url=base_url,
            api_key=api_key or os.environ.get("LANGCHAIN_API_KEY", ""),
        )

    def trace_llm(self, *, name: str, provider: str, model: str, prompt: str, completion: str,
                  prompt_tokens: int, completion_tokens: int, duration: float,
                  task_id: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        import datetime
        from langsmith.schemas import RunTypeEnum  # type: ignore[import]
        import uuid

        run_id = uuid.uuid4()
        now = datetime.datetime.utcnow()
        self._client.create_run(
            id=run_id,
            name=name,
            run_type=RunTypeEnum.llm,
            project_name=self._project,
            session_id=task_id,
            inputs={"prompt": prompt},
            outputs={"completion": completion},
            extra={
                "metadata": {**(metadata or {}), "provider": provider, "model": model,
                             "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            },
            start_time=now,
            end_time=now,
        )

    def trace_tool(self, *, name: str, tool: str, inputs: dict[str, Any], output: str,
                   success: bool, duration: float, task_id: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> None:
        import datetime
        from langsmith.schemas import RunTypeEnum  # type: ignore[import]
        import uuid

        run_id = uuid.uuid4()
        now = datetime.datetime.utcnow()
        self._client.create_run(
            id=run_id,
            name=tool,
            run_type=RunTypeEnum.tool,
            project_name=self._project,
            session_id=task_id,
            inputs=inputs,
            outputs={"result": output, "success": success},
            extra={"metadata": {**(metadata or {}), "duration_s": round(duration, 3)}},
            error=None if success else output,
            start_time=now,
            end_time=now,
        )

    def trace_task(self, *, task_id: str, request: str, status: str, stage: str,
                   duration: float, metadata: Optional[dict[str, Any]] = None) -> None:
        import datetime
        from langsmith.schemas import RunTypeEnum  # type: ignore[import]
        import uuid

        now = datetime.datetime.utcnow()
        self._client.create_run(
            id=uuid.uuid4(),
            name="task",
            run_type=RunTypeEnum.chain,
            project_name=self._project,
            session_id=task_id,
            inputs={"request": request},
            outputs={"status": status, "stage": stage},
            extra={"metadata": {**(metadata or {}), "duration_s": round(duration, 3)}},
            start_time=now,
            end_time=now,
        )

    def flush(self) -> None:
        pass  # langsmith client flushes automatically
