"""OpenCode adapter: drives the ``opencode`` CLI in non-interactive ``run`` mode."""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from adapters.claude.provider import _response_from_text
from agent.providers.base import ModelRequest, ModelResponse, ProviderError
from tools.shell import run_command, which


class OpenCodeProvider:
    name = "opencode"

    def __init__(self, model: Optional[str] = None, binary: str = "opencode", timeout: int = 300) -> None:
        self.model = model or os.environ.get("OPENCODE_MODEL", "default")
        self.binary = binary
        self.timeout = timeout

    def available(self) -> bool:
        return which(self.binary) is not None

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.available():
            raise ProviderError("opencode CLI not found on PATH", kind="unavailable")
        argv = [self.binary, "run", "--format", "json"]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        prompt = request.as_prompt_text()
        started = time.time()
        out = run_command(argv + [prompt], timeout=self.timeout,
                          env_passthrough=("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENCODE_CONFIG"))
        if not out.ok:
            raise ProviderError(f"opencode CLI failed: {out.stderr[:300] or out.stdout[:300]}", kind="unknown")
        text = out.stdout
        # opencode may emit one JSON event per line; keep the final text
        texts = []
        for line in out.stdout.splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                part = evt.get("text") or evt.get("content") or (evt.get("part") or {}).get("text")
                if part:
                    texts.append(str(part))
        if texts:
            text = "\n".join(texts)
        return _response_from_text(text, self.name, self.model, 0, 0, time.time() - started)
