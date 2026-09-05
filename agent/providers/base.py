"""Model provider abstraction.

The harness never imports a vendor SDK. A provider receives a
``ModelRequest`` (system prompt, messages, optional tool definitions) and
returns a ``ModelResponse`` (text and/or structured tool calls). Providers
that cannot do native tool calling (coding agents driven through a CLI)
return JSON in text which the decider parses.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ModelMessage:
    role: str  # system|user|assistant|tool
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass
class ModelToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class ModelRequest:
    system: str
    messages: list[ModelMessage]
    tools: list[dict[str, Any]] = field(default_factory=list)  # {name, description, input_schema}
    max_tokens: int = 2048
    temperature: float = 0.0
    json_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_prompt_text(self) -> str:
        """Flatten to a single prompt for CLI-driven agents without a chat API."""
        parts = [f"SYSTEM:\n{self.system}"]
        if self.tools:
            parts.append("AVAILABLE TOOLS (respond with JSON {\"action\":\"tool\",\"tool\":name,\"args\":{...}} to call one):\n" +
                         "\n".join(f"- {t['name']}: {t.get('description', '')[:200]}" for t in self.tools))
        for m in self.messages:
            parts.append(f"{m.role.upper()}:\n{m.content}")
        return "\n\n".join(parts)


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = None
    stop_reason: str = ""

    def parsed_json(self) -> Optional[dict[str, Any]]:
        return extract_json(self.text)


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    model: str

    def available(self) -> bool: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class ProviderError(Exception):
    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Find the first JSON object in free text (handles ```json fences)."""
    if not text:
        return None
    candidates = []
    fence = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates.extend(fence)
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    # greedy first { ... last }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class NullProvider:
    """Provider used when no model is configured. Always unavailable."""

    name = "none"
    model = "none"

    def available(self) -> bool:
        return False

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise ProviderError("no model provider configured", kind="unavailable")


class MockProvider:
    """Deterministic provider for tests and --mock runs.

    ``script`` is a list of responses returned in order; when exhausted (or
    empty) the provider returns a generic completion that asks the harness to
    finish with the evidence it has.
    """

    name = "mock"
    model = "mock-1"

    def __init__(self, script: Optional[list[ModelResponse | dict[str, Any] | str]] = None) -> None:
        self.script = list(script or [])
        self.requests: list[ModelRequest] = []

    def available(self) -> bool:
        return True

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        prompt_tokens = sum(len(m.content) for m in request.messages) // 4 + len(request.system) // 4
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, ModelResponse):
                item.provider, item.model = self.name, self.model
                item.prompt_tokens = item.prompt_tokens or prompt_tokens
                return item
            if isinstance(item, dict):
                if "tool" in item and "args" in item:
                    return ModelResponse(text=json.dumps(item), tool_calls=[ModelToolCall(item["tool"], item["args"], id="mock")],
                                         provider=self.name, model=self.model, prompt_tokens=prompt_tokens, completion_tokens=20)
                return ModelResponse(text=json.dumps(item), provider=self.name, model=self.model,
                                     prompt_tokens=prompt_tokens, completion_tokens=20)
            return ModelResponse(text=str(item), provider=self.name, model=self.model, prompt_tokens=prompt_tokens, completion_tokens=20)
        text = json.dumps({"action": "complete", "summary": "Mock provider: no scripted response; finishing with collected evidence."})
        return ModelResponse(text=text, provider=self.name, model=self.model, prompt_tokens=prompt_tokens, completion_tokens=16)
