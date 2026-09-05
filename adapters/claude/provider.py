"""Claude adapters.

* ``AnthropicProvider``  - Anthropic Messages API over urllib (native tool use).
* ``ClaudeCodeProvider`` - drives the ``claude`` CLI in non-interactive
  (``-p``) mode so Claude Code itself can act as the reasoning engine while
  the harness keeps policy/approval/audit control. Tool calls come back as
  JSON in text, which the decider parses.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from agent.providers.base import ModelRequest, ModelResponse, ModelToolCall, ProviderError
from tools.shell import run_command, which


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None, api_key_env: str = "ANTHROPIC_API_KEY",
                 timeout: int = 180) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout

    def available(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError(f"{self.api_key_env} is not set", kind="auth")
        messages: list[dict[str, Any]] = []
        for m in request.messages:
            if m.role == "tool":
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id or "call",
                                                              "content": m.content}]})
            else:
                messages.append({"role": "assistant" if m.role == "assistant" else "user", "content": m.content})
        body: dict[str, Any] = {"model": self.model, "max_tokens": request.max_tokens, "system": request.system,
                                "messages": messages, "temperature": request.temperature}
        if request.tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t.get("input_schema") or {"type": "object", "properties": {}}} for t in request.tools]
        req = urllib.request.Request(f"{self.base_url}/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            kind = "auth" if exc.code in (401, 403) else "rate_limit" if exc.code == 429 else "network"
            raise ProviderError(f"Anthropic API error {exc.code}: {detail}", kind=kind) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"cannot reach {self.base_url}: {exc.reason}", kind="network") from exc
        text_parts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ModelToolCall(block.get("name", ""), block.get("input") or {}, id=block.get("id", "")))
        usage = data.get("usage", {})
        return ModelResponse(text="\n".join(text_parts), tool_calls=calls, provider=self.name, model=self.model,
                             prompt_tokens=int(usage.get("input_tokens", 0)), completion_tokens=int(usage.get("output_tokens", 0)),
                             raw={"duration": time.time() - started}, stop_reason=str(data.get("stop_reason", "")))


class ClaudeCodeProvider:
    """Uses the Claude Code CLI as the model. Requires ``claude`` on PATH."""

    name = "claude-code"

    def __init__(self, model: Optional[str] = None, binary: str = "claude", timeout: int = 300) -> None:
        self.model = model or os.environ.get("CLAUDE_CODE_MODEL", "default")
        self.binary = binary
        self.timeout = timeout

    def available(self) -> bool:
        return which(self.binary) is not None

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.available():
            raise ProviderError("claude CLI not found on PATH", kind="unavailable")
        argv = [self.binary, "-p", "--output-format", "json", "--append-system-prompt", request.system]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        prompt = request.as_prompt_text() if request.tools else "\n\n".join(f"{m.role.upper()}:\n{m.content}" for m in request.messages)
        started = time.time()
        out = run_command(argv, timeout=self.timeout, input_text=prompt, env_passthrough=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"))
        if not out.ok:
            raise ProviderError(f"claude CLI failed: {out.stderr[:300] or out.stdout[:300]}", kind="unknown")
        text, usage = out.stdout, {}
        try:
            data = json.loads(out.stdout)
            if isinstance(data, dict):
                text = data.get("result") or data.get("content") or out.stdout
                usage = data.get("usage") or {}
        except json.JSONDecodeError:
            pass
        return _response_from_text(text, self.name, self.model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                                   time.time() - started)


def _response_from_text(text: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int, duration: float) -> ModelResponse:
    from agent.providers.base import extract_json

    resp = ModelResponse(text=text, provider=provider, model=model, prompt_tokens=int(prompt_tokens or 0),
                         completion_tokens=int(completion_tokens or 0), raw={"duration": duration})
    obj = extract_json(text)
    if obj and obj.get("action") == "tool" and obj.get("tool"):
        resp.tool_calls = [ModelToolCall(str(obj["tool"]), dict(obj.get("args") or {}), id="cli")]
    return resp
