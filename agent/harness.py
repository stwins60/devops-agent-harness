"""Harness: wires configuration, policy, approvals, audit, tools, memory, runbooks, providers and specialists together."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from adapters.generic.mcp import McpConnections
from agent.approvals.engine import ApprovalEngine, ApprovalHandler, build_handler
from agent.audit.logger import AuditLogger, Metrics
from agent.config import EnvironmentBinding, HarnessConfig
from agent.context.collector import ContextEngine
from agent.executor import ToolExecutor
from agent.memory.store import MemoryStore
from agent.models import OperatingMode, TaskKind
from agent.policies.engine import PolicyEngine, load_policy
from agent.providers.base import ModelProvider
from agent.providers.factory import build_provider
from agent.rca.engine import RootCauseEngine
from agent.runbooks.loader import BUILTIN_RUNBOOK_DIR, RunbookLibrary
from agent.specialists.base import Specialist
from agent.state.store import TaskState, TaskStore
from tools.catalog import build_backends, build_registry
from tools.mock.world import MockWorld


class Harness:
    def __init__(self, config: HarnessConfig, *, approval_handler: Optional[ApprovalHandler] = None, world: Optional[MockWorld] = None,
                 provider: Optional[ModelProvider] = None, echo_audit: bool = False) -> None:
        self.config = config
        if config.mock and not config.environment_bindings:
            # the mock cluster is bound to production so policies behave realistically in demos and tests
            config.environment_bindings = [EnvironmentBinding("production", kube_contexts=["mock-cluster"], namespaces=["production"], aws_accounts=["123456789012"]),
                                           EnvironmentBinding("staging", namespaces=["staging"]), EnvironmentBinding("dev", kube_contexts=["kind-dev"], namespaces=["dev"])]
        if config.mock and config.default_namespace == "default":
            config.default_namespace = "production"
        config.agent_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLogger(config.agent_dir / "audit" / "audit.jsonl", Metrics(), echo=echo_audit)
        self.policy = PolicyEngine(load_policy(config.project_root))
        for k, v in self.policy.policy.limits.items():
            if hasattr(config.limits, k):
                setattr(config.limits, k, min(getattr(config.limits, k), int(v)))
        self.registry = build_registry(config)
        self.backends = build_backends(config, world)
        self.world: Optional[MockWorld] = self.backends.get("world")
        if self.world is not None:
            config.github_repo = config.github_repo or self.world.github["repo"]
            config.gitlab_project = config.gitlab_project or self.world.gitlab["project"]
            self.world.flags.update(config.extra.get("mock_flags") or {})
        self.store = TaskStore(config.tasks_dir)
        handler = approval_handler or build_handler(interactive=not config.non_interactive, auto_approve=config.auto_approve,
                                                    allow_explicit=bool(config.extra.get("approve_all")))
        self.approvals = ApprovalEngine(handler)
        self.executor = ToolExecutor(self.registry, self.policy, self.approvals, self.audit, config, self.backends, self.store)
        self.memory = MemoryStore(config.agent_dir)
        self.runbooks = RunbookLibrary([BUILTIN_RUNBOOK_DIR, config.agent_dir / "runbooks", config.project_root / "runbooks"])
        self.rca = RootCauseEngine()
        self.provider: ModelProvider = provider or build_provider(config.provider, config.provider_model, mock=config.mock)
        self.mcp = McpConnections()
        if config.mcp_servers:
            self.mcp.connect_all(config.mcp_servers, self.registry, cwd=config.project_root)
        self.specialists: dict[str, Specialist] = {}
        self._register_specialists()
        self.context = ContextEngine(self)
        from agent.orchestrator.orchestrator import Orchestrator

        self.orchestrator = Orchestrator(self)

    def _register_specialists(self) -> None:
        from agent.specialists.ansible import AnsibleSpecialist
        from agent.specialists.aws import AwsSpecialist
        from agent.specialists.cicd import CiCdSpecialist
        from agent.specialists.docker import DockerSpecialist
        from agent.specialists.documentation import DocumentationSpecialist
        from agent.specialists.git import GitSpecialist
        from agent.specialists.incident import IncidentSpecialist
        from agent.specialists.jira import JiraSpecialist
        from agent.specialists.kubernetes import KubernetesSpecialist
        from agent.specialists.linux import LinuxSpecialist
        from agent.specialists.networking import NetworkingSpecialist
        from agent.specialists.observability import ObservabilitySpecialist
        from agent.specialists.security import SecuritySpecialist
        from agent.specialists.terraform import TerraformSpecialist

        for cls in (KubernetesSpecialist, DockerSpecialist, LinuxSpecialist, JiraSpecialist, GitSpecialist, CiCdSpecialist, AwsSpecialist, TerraformSpecialist,
                    AnsibleSpecialist, NetworkingSpecialist, ObservabilitySpecialist, SecuritySpecialist, IncidentSpecialist, DocumentationSpecialist):
            self.register_specialist(cls(self))

    def register_specialist(self, specialist: Specialist) -> None:
        """Extension point: add a specialist without touching the orchestrator."""
        self.specialists[specialist.name] = specialist

    # -- public API ---------------------------------------------------------
    def run(self, request: str, *, kind: Optional[TaskKind] = None, task_id: Optional[str] = None, mode: Optional[OperatingMode] = None,
            dry_run: Optional[bool] = None, repo: Optional[Path] = None, progress=None) -> TaskState:
        return self.orchestrator.start(request, kind=kind, task_id=task_id, mode=mode, dry_run=dry_run, repo=repo, progress=progress)

    def resume(self, task_id: str, *, progress=None) -> TaskState:
        return self.orchestrator.resume(task_id, progress=progress)

    def close(self) -> None:
        self.mcp.close()

    def summary(self) -> dict[str, Any]:
        return {"mock": self.config.mock, "scenario": self.config.mock_scenario if self.config.mock else None, "mode": self.config.mode.value,
                "environment": self.config.environment.value, "provider": f"{self.provider.name} ({'available' if self.provider.available() else 'unavailable'})",
                "tools": len(self.registry), "specialists": sorted(self.specialists), "runbooks": len(self.runbooks.runbooks),
                "runbook_errors": self.runbooks.errors, "mcp_errors": self.mcp.errors}
