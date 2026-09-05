"""GitHub Copilot adapter: drives the Copilot CLI (``copilot`` or ``gh copilot``) non-interactively."""
from __future__ import annotations

import os
import time
from typing import Optional

from adapters.claude.provider import _response_from_text
from agent.providers.base import ModelRequest, ModelResponse, ProviderError
from tools.shell import run_command, which


class CopilotProvider:
    name = "copilot"

    def __init__(self, model: Optional[str] = None, timeout: int = 300) -> None:
        self.model = model or os.environ.get("COPILOT_MODEL", "default")
        self.timeout = timeout

    def _argv(self) -> Optional[list[str]]:
        if which("copilot"):
            argv = ["copilot", "-p"]
            if self.model and self.model != "default":
                argv += ["--model", self.model]
            return argv
        if which("gh"):
            return ["gh", "copilot", "explain"]  # limited fallback: explain-only mode
        return None

    def available(self) -> bool:
        return self._argv() is not None

    def complete(self, request: ModelRequest) -> ModelResponse:
        argv = self._argv()
        if argv is None:
            raise ProviderError("neither 'copilot' nor 'gh copilot' found on PATH", kind="unavailable")
        prompt = request.as_prompt_text()
        started = time.time()
        out = run_command(argv + [prompt], timeout=self.timeout, env_passthrough=("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_TOKEN"))
        if not out.ok:
            raise ProviderError(f"copilot CLI failed: {out.stderr[:300] or out.stdout[:300]}", kind="unknown")
        return _response_from_text(out.stdout, self.name, self.model, 0, 0, time.time() - started)
