"""Subprocess runner shared by every CLI-backed tool.

* never uses ``shell=True`` unless explicitly requested (pipelines)
* enforces timeouts
* captures and redacts stdout/stderr
* strips secret-looking environment variables from the child environment
  unless they are explicitly allowlisted by the backend (e.g. AWS_PROFILE)
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from agent.audit.redaction import redact_text
from tools.base import ToolError

_ENV_ALLOWLIST_PREFIXES = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_", "TERM", "SHELL",
                           "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                           "KUBECONFIG", "DOCKER_HOST", "DOCKER_CONFIG", "TF_", "ANSIBLE_", "GIT_", "SSH_AUTH_SOCK", "PYTHON",
                           "VIRTUAL_ENV", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
                           "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME", "NO_COLOR", "PYTHONPATH", "PYTHONIOENCODING")
_ENV_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL")


def child_environment(passthrough: Sequence[str] = ()) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in passthrough:
            env[key] = value
        elif any(m in upper for m in _ENV_SECRET_MARKERS):
            continue
        elif upper.startswith(_ENV_ALLOWLIST_PREFIXES) or upper in ("USER", "LOGNAME", "PWD", "OS"):
            env[key] = value
    # git must never prompt
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


@dataclass
class CommandOutput:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {"command": self.command, "returncode": self.returncode, "stdout": self.stdout,
                "stderr": self.stderr, "duration": round(self.duration, 3), "timed_out": self.timed_out}


def which(program: str) -> Optional[str]:
    return shutil.which(program)


def run_command(argv: Sequence[str] | str, *, cwd: Optional[Path] = None, timeout: int = 120,
                env_passthrough: Sequence[str] = (), input_text: Optional[str] = None, shell: bool = False,
                max_output: int = 200_000) -> CommandOutput:
    """Run a command and return redacted output. Raises ToolError if the binary is missing."""
    if isinstance(argv, str):
        command_str = argv
        args: Sequence[str] | str = argv if shell else shlex.split(argv, posix=(os.name != "nt"))
    else:
        args = list(argv)
        command_str = " ".join(shlex.quote(a) for a in args)
    if not shell:
        prog = args[0] if isinstance(args, list) else str(args).split()[0]
        if which(prog) is None and not Path(prog).exists():
            raise ToolError(f"required binary '{prog}' is not installed or not on PATH", kind="unavailable",
                            advice=f"install '{prog}' or configure a backend/MCP server that provides it")
    started = time.time()
    try:
        proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              timeout=timeout, env=child_environment(env_passthrough), input=input_text,
                              shell=shell, encoding="utf-8", errors="replace")
        out = CommandOutput(command_str, proc.returncode, _trim(proc.stdout, max_output), _trim(proc.stderr, max_output),
                            time.time() - started, argv=list(args) if isinstance(args, list) else [])
    except subprocess.TimeoutExpired as exc:
        out = CommandOutput(command_str, -1, _trim(_as_text(exc.stdout), max_output), _trim(_as_text(exc.stderr), max_output),
                            time.time() - started, timed_out=True)
    out.stdout = redact_text(out.stdout)
    out.stderr = redact_text(out.stderr)
    return out


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _trim(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n... [{len(text) - limit} chars truncated] ...\n" + text[-limit // 2:]
