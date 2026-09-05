"""OpenAI-compatible chat completions provider (OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, OpenRouter...).

Only urllib is used. The API key is read from the environment at call time
and never stored on the object in plain form beyond the request header.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from agent.providers.base import ModelRequest, ModelResponse, ModelToolCall, ProviderError


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None, api_key_env: str = "OPENAI_API_KEY",
                 timeout: int = 120, extra_headers: Optional[dict[str, str]] = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def _api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)

    def available(self) -> bool:
        # local OpenAI-compatible servers (Ollama) don't need a key
        return bool(self._api_key()) or "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def complete(self, request: ModelRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for m in request.messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool" and m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            messages.append(entry)
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": request.temperature,
                                "max_tokens": request.max_tokens}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
                             for t in request.tools]
        if request.json_mode and not request.tools:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", **self.extra_headers}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            kind = "auth" if exc.code in (401, 403) else "rate_limit" if exc.code == 429 else "network"
            raise ProviderError(f"OpenAI-compatible API error {exc.code}: {detail}", kind=kind) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"cannot reach {self.base_url}: {exc.reason}", kind="network") from exc
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ModelToolCall(fn.get("name", ""), args, id=tc.get("id", "")))
        usage = data.get("usage", {})
        return ModelResponse(text=msg.get("content") or "", tool_calls=calls, provider=self.name, model=self.model,
                             prompt_tokens=int(usage.get("prompt_tokens", 0)), completion_tokens=int(usage.get("completion_tokens", 0)),
                             raw={"duration": time.time() - started, "finish_reason": choice.get("finish_reason")},
                             stop_reason=str(choice.get("finish_reason", "")))
