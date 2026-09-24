"""Observability tracing for the devops-agent harness.

Tracing is opt-in and backend-agnostic. Any object implementing the
``Tracer`` protocol can be used. Built-in backends:

- ``langfuse``   - LangFuse OSS / cloud
- ``langsmith``  - LangSmith (LangChain)
- ``litellm``    - LiteLLM proxy (records via its built-in callbacks)
- ``null``       - no-op (default when tracing is disabled)

Configure in ``.agent/config.yaml``::

    tracing:
      backend: langfuse          # langfuse | langsmith | litellm | null
      base_url: https://cloud.langfuse.com
      public_key: pk-lf-...      # from env LANGFUSE_PUBLIC_KEY if omitted
      secret_key: sk-lf-...      # from env LANGFUSE_SECRET_KEY if omitted
      project: devops-agent      # used as project name / tags
      enabled: true
"""
from __future__ import annotations

from agent.tracing.base import Span, Tracer, NullTracer
from agent.tracing.factory import build_tracer
from agent.tracing.multi import MultiTracer

__all__ = ["Span", "Tracer", "NullTracer", "MultiTracer", "build_tracer"]
