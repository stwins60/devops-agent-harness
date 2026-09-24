"""Tracer factory — builds the correct backend(s) from HarnessConfig.

Single backend::

    tracing:
      backend: langfuse
      enabled: true
      base_url: http://langfuse.internal:3000   # self-hosted
      public_key: pk-lf-...
      secret_key: sk-lf-...
      project: devops-agent

Multiple backends (fan-out via MultiTracer)::

    tracing:
      enabled: true
      backends:
        - backend: langfuse
          base_url: http://langfuse.internal:3000
          public_key: pk-lf-...
          secret_key: sk-lf-...
        - backend: langsmith
          base_url: http://langsmith.internal:1984
          api_key: ls__...
        - backend: litellm
          base_url: http://litellm-proxy:4000
          success_callback: ["langfuse"]
"""
from __future__ import annotations

import warnings
from typing import Any, TYPE_CHECKING

from agent.tracing.base import NullTracer, Tracer

if TYPE_CHECKING:
    from agent.config import HarnessConfig


def _build_single(cfg: dict[str, Any], project: str) -> Tracer:
    """Build one backend from a dict with a 'backend' key."""
    backend = str(cfg.get("backend", "null")).lower()
    base_url = cfg.get("base_url")

    if backend == "langfuse":
        from agent.tracing.langfuse import LangfuseTracer
        return LangfuseTracer(
            base_url=base_url or "https://cloud.langfuse.com",
            public_key=cfg.get("public_key"),
            secret_key=cfg.get("secret_key"),
            project=cfg.get("project", project),
        )

    if backend == "langsmith":
        from agent.tracing.langsmith import LangSmithTracer
        return LangSmithTracer(
            base_url=base_url or "https://api.smith.langchain.com",
            api_key=cfg.get("api_key"),
            project=cfg.get("project", project),
        )

    if backend == "litellm":
        from agent.tracing.litellm import LiteLLMTracer
        return LiteLLMTracer(
            base_url=base_url,
            success_callback=cfg.get("success_callback"),
            failure_callback=cfg.get("failure_callback"),
            project=cfg.get("project", project),
        )

    if backend not in ("null", "none", ""):
        warnings.warn(f"Unknown tracing backend '{backend}'; skipping.", stacklevel=3)
    return NullTracer()


def build_tracer(config: "HarnessConfig") -> Tracer:
    """Instantiate the configured tracing backend(s). Returns NullTracer if disabled."""
    tracing: dict[str, Any] = config.extra.get("tracing") or {}
    if not tracing or not tracing.get("enabled", True):
        return NullTracer()

    project = str(tracing.get("project", "devops-agent"))

    # Multi-backend fan-out: tracing.backends is a list
    backends_list = tracing.get("backends")
    if backends_list and isinstance(backends_list, list):
        from agent.tracing.multi import MultiTracer
        tracers = [_build_single({**tracing, **b}, project) for b in backends_list]
        active = [t for t in tracers if not isinstance(t, NullTracer)]
        if not active:
            return NullTracer()
        if len(active) == 1:
            return active[0]
        return MultiTracer(active)

    # Single backend: tracing.backend is a string
    return _build_single(tracing, project)
