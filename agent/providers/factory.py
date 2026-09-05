"""Provider factory: pick a model provider from configuration without importing vendor SDKs."""
from __future__ import annotations

import os
from typing import Optional

from agent.providers.base import ModelProvider, MockProvider, NullProvider

_ORDER = ("anthropic", "openai", "claude-code", "opencode", "copilot")


def build_provider(name: str = "auto", model: Optional[str] = None, *, mock: bool = False) -> ModelProvider:
    name = (name or "auto").lower()
    if mock or name == "mock":
        return MockProvider()
    if name in ("none", "off", "disabled"):
        return NullProvider()
    if name == "auto":
        for candidate in _ORDER:
            provider = _construct(candidate, model)
            if provider is not None and provider.available():
                return provider
        return NullProvider()
    provider = _construct(name, model)
    if provider is None:
        raise ValueError(f"unknown provider '{name}' (expected one of: mock, none, auto, {', '.join(_ORDER)})")
    return provider


def _construct(name: str, model: Optional[str]) -> Optional[ModelProvider]:
    if name in ("openai", "openai-compatible", "ollama", "azure"):
        from adapters.openai.provider import OpenAICompatibleProvider

        base = os.environ.get("OPENAI_BASE_URL")
        if name == "ollama":
            base = base or "http://localhost:11434/v1"
        return OpenAICompatibleProvider(model=model, base_url=base)
    if name == "anthropic":
        from adapters.claude.provider import AnthropicProvider

        return AnthropicProvider(model=model)
    if name in ("claude-code", "claude"):
        from adapters.claude.provider import ClaudeCodeProvider

        return ClaudeCodeProvider(model=model)
    if name == "opencode":
        from adapters.opencode.provider import OpenCodeProvider

        return OpenCodeProvider(model=model)
    if name == "copilot":
        from adapters.copilot.provider import CopilotProvider

        return CopilotProvider(model=model)
    return None
