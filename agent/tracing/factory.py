"""Tracer factory — builds the correct backend from HarnessConfig."""
from __future__ import annotations

import warnings
from typing import Any, TYPE_CHECKING

from agent.tracing.base import NullTracer, Tracer

if TYPE_CHECKING:
    from agent.config import HarnessConfig


def build_tracer(config: "HarnessConfig") -> Tracer:
    """Instantiate the configured tracing backend. Returns NullTracer if disabled."""
    tracing: dict[str, Any] = config.extra.get("tracing") or {}
    if not tracing or not tracing.get("enabled", True):
        return NullTracer()

    backend = str(tracing.get("backend", "null")).lower()
    project = str(tracing.get("project", "devops-agent"))
    base_url = tracing.get("base_url")

    if backend == "langfuse":
        from agent.tracing.langfuse import LangfuseTracer
        return LangfuseTracer(
            base_url=base_url or "https://cloud.langfuse.com",
            public_key=tracing.get("public_key"),
            secret_key=tracing.get("secret_key"),
            project=project,
        )

    if backend == "langsmith":
        from agent.tracing.langsmith import LangSmithTracer
        return LangSmithTracer(
            base_url=base_url or "https://api.smith.langchain.com",
            api_key=tracing.get("api_key"),
            project=project,
        )

    if backend == "litellm":
        from agent.tracing.litellm import LiteLLMTracer
        return LiteLLMTracer(
            base_url=base_url,
            success_callback=tracing.get("success_callback"),
            failure_callback=tracing.get("failure_callback"),
            project=project,
        )

    if backend not in ("null", "none", ""):
        warnings.warn(f"Unknown tracing backend '{backend}'; tracing disabled.", stacklevel=2)

    return NullTracer()
