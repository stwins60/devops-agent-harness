"""Harness configuration.

Configuration is layered (lowest to highest precedence):

1. built-in defaults
2. ``.agent/config.yaml`` in the project root (trusted, checked into the repo)
3. environment variables prefixed ``DEVOPS_AGENT_``
4. CLI flags

Secrets are *never* stored here; tokens are resolved lazily from the
environment or a credential provider at the moment a backend needs them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models import Environment, OperatingMode


@dataclass
class Limits:
    max_tool_calls: int = 200
    max_iterations: int = 60
    max_repeated_calls: int = 3
    default_timeout: int = 120
    max_context_chars: int = 12000
    max_retries_transient: int = 2


@dataclass
class EnvironmentBinding:
    """Trusted mapping from infrastructure identity to an environment."""

    name: str
    kube_contexts: list[str] = field(default_factory=list)
    aws_accounts: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)


@dataclass
class HarnessConfig:
    project_root: Path = field(default_factory=lambda: Path.cwd())
    agent_dir: Path = field(default_factory=lambda: Path.cwd() / ".agent")
    tasks_dir: Path = field(default_factory=lambda: Path.cwd() / "tasks")
    mock: bool = False
    mock_scenario: str = "probe-port-mismatch"
    mode: OperatingMode = OperatingMode.APPROVAL
    environment: Environment = Environment.UNKNOWN
    environment_source: str = "default"  # config|env|flag|default|detected
    environment_bindings: list[EnvironmentBinding] = field(default_factory=list)
    provider: str = "auto"  # auto|mock|openai|anthropic|claude-code|opencode|copilot|none
    provider_model: Optional[str] = None
    dry_run: bool = False
    non_interactive: bool = False
    auto_approve: bool = False
    jira_url: Optional[str] = None
    jira_project: Optional[str] = None
    github_repo: Optional[str] = None  # owner/repo
    gitlab_project: Optional[str] = None
    git_provider: str = "github"
    default_namespace: str = "default"
    default_repo_path: Optional[str] = None
    kube_context: Optional[str] = None
    aws_profile: Optional[str] = None
    aws_region: Optional[str] = None
    prometheus_url: Optional[str] = None
    loki_url: Optional[str] = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    mcp_preapproved: list[str] = field(default_factory=list)  # tools (globs) pre-approved when serving over MCP
    limits: Limits = field(default_factory=Limits)
    pricing: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, project_root: Optional[Path] = None, overrides: Optional[dict[str, Any]] = None) -> "HarnessConfig":
        root = Path(project_root or os.environ.get("DEVOPS_AGENT_PROJECT_ROOT") or Path.cwd()).resolve()
        cfg = cls(project_root=root, agent_dir=root / ".agent", tasks_dir=root / "tasks")
        cfg._apply_file(root / ".agent" / "config.yaml")
        cfg._apply_env()
        if overrides:
            cfg._apply(overrides, source="flag")
        return cfg

    def _apply_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(f"Invalid config file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Config file {path} must contain a mapping")
        self._apply(data, source="config")

    def _apply_env(self) -> None:
        env = os.environ
        mapping = {
            "DEVOPS_AGENT_MOCK": ("mock", lambda v: v.lower() in ("1", "true", "yes")),
            "DEVOPS_AGENT_MOCK_SCENARIO": ("mock_scenario", str),
            "DEVOPS_AGENT_MODE": ("mode", str),
            "DEVOPS_AGENT_ENV": ("environment", str),
            "DEVOPS_AGENT_PROVIDER": ("provider", str),
            "DEVOPS_AGENT_MODEL": ("provider_model", str),
            "DEVOPS_AGENT_JIRA_URL": ("jira_url", str),
            "JIRA_URL": ("jira_url", str),
            "DEVOPS_AGENT_GITHUB_REPO": ("github_repo", str),
            "DEVOPS_AGENT_GITLAB_PROJECT": ("gitlab_project", str),
            "DEVOPS_AGENT_NAMESPACE": ("default_namespace", str),
            "DEVOPS_AGENT_KUBE_CONTEXT": ("kube_context", str),
            "AWS_PROFILE": ("aws_profile", str),
            "AWS_REGION": ("aws_region", str),
            "DEVOPS_AGENT_PROMETHEUS_URL": ("prometheus_url", str),
            "DEVOPS_AGENT_LOKI_URL": ("loki_url", str),
            "DEVOPS_AGENT_TASKS_DIR": ("tasks_dir", str),
            "DEVOPS_AGENT_NON_INTERACTIVE": ("non_interactive", lambda v: v.lower() in ("1", "true", "yes")),
        }
        data: dict[str, Any] = {}
        for var, (key, conv) in mapping.items():
            if var in env and env[var] != "":
                data[key] = conv(env[var])
        if data:
            self._apply(data, source="env")

    def _apply(self, data: dict[str, Any], source: str) -> None:
        for key, value in data.items():
            if value is None:
                continue
            if key == "mode":
                self.mode = OperatingMode.parse(value)
            elif key == "environment":
                self.environment = Environment.parse(value)
                self.environment_source = source
            elif key == "environments":
                self.environment_bindings = [
                    EnvironmentBinding(name=str(name), **{k: list(v) for k, v in (spec or {}).items()
                                                         if k in EnvironmentBinding.__dataclass_fields__ and k != "name"})
                    for name, spec in (value or {}).items()
                ]
            elif key == "limits":
                for lk, lv in (value or {}).items():
                    if hasattr(self.limits, lk):
                        setattr(self.limits, lk, int(lv))
            elif key in ("tasks_dir", "agent_dir"):
                p = Path(value)
                setattr(self, key, p if p.is_absolute() else self.project_root / p)
            elif key == "mcp_servers":
                self.mcp_servers = list(value or [])
            elif key == "pricing":
                self.pricing = dict(value or {})
            elif hasattr(self, key):
                setattr(self, key, value)
            else:
                self.extra[key] = value

    # -- helpers ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["project_root"] = str(self.project_root)
        d["agent_dir"] = str(self.agent_dir)
        d["tasks_dir"] = str(self.tasks_dir)
        d["mode"] = self.mode.value
        d["environment"] = self.environment.value
        return d

    def environment_for(self, *, kube_context: Optional[str] = None, aws_account: Optional[str] = None,
                        namespace: Optional[str] = None, host: Optional[str] = None,
                        branch: Optional[str] = None) -> Optional[Environment]:
        """Resolve an environment from trusted bindings only. Returns None if unknown."""
        for b in self.environment_bindings:
            if kube_context and kube_context in b.kube_contexts:
                return Environment.parse(b.name)
            if aws_account and aws_account in b.aws_accounts:
                return Environment.parse(b.name)
            if namespace and namespace in b.namespaces:
                return Environment.parse(b.name)
            if host and host in b.hosts:
                return Environment.parse(b.name)
            if branch and branch in b.branches:
                return Environment.parse(b.name)
        return None
