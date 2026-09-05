"""Context engine: gathers repository, instructions, environment, ticket, cluster and memory context
and summarises it within a character budget before anything reaches a model."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agent.context.agents_md import InstructionSet, discover
from agent.context.environment import EnvironmentResolution, resolve_environment
from agent.models import Environment

if TYPE_CHECKING:  # pragma: no cover
    from agent.harness import Harness
    from agent.specialists.base import Investigation


@dataclass
class ContextBundle:
    repository: dict[str, Any] = field(default_factory=dict)
    instructions: Optional[InstructionSet] = None
    environment: Optional[EnvironmentResolution] = None
    jira: dict[str, Any] = field(default_factory=dict)
    kubernetes: dict[str, Any] = field(default_factory=dict)
    memory: str = ""
    runbooks: list[str] = field(default_factory=list)
    recent_changes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository, "instruction_files": self.instructions.paths if self.instructions else [],
            "environment": {"value": self.environment.environment.value, "source": self.environment.source, "evidence": self.environment.evidence} if self.environment else None,
            "jira": {k: v for k, v in self.jira.items() if k in ("key", "summary", "status", "priority", "repository", "service", "namespace")},
            "kubernetes": self.kubernetes, "runbooks": self.runbooks, "recent_changes": self.recent_changes[:5], "notes": self.notes,
        }

    def summarize(self, max_chars: int = 6000) -> str:
        parts = []
        if self.environment:
            parts.append(f"Environment: {self.environment.environment.value} (source: {self.environment.source})")
        if self.repository:
            r = self.repository
            parts.append(f"Repository: {r.get('path')} branch={r.get('branch')} remote={r.get('remote')} clean={r.get('clean')}")
        if self.recent_changes:
            parts.append("Recent commits:\n" + "\n".join(f"  - {c.get('sha', '')[:7]} {c.get('message', '')[:80]}" for c in self.recent_changes[:5]))
        if self.jira:
            j = self.jira
            parts.append(f"Jira {j.get('key')}: {j.get('summary')} [{j.get('status')}, {j.get('priority')}]\n{str(j.get('description', ''))[:800]}")
        if self.kubernetes:
            parts.append("Kubernetes: " + ", ".join(f"{k}={v}" for k, v in self.kubernetes.items()))
        if self.runbooks:
            parts.append("Matching runbooks: " + ", ".join(self.runbooks))
        if self.memory:
            parts.append("Project memory:\n" + self.memory)
        if self.instructions and self.instructions.files:
            parts.append("Instructions (AGENTS.md):\n" + self.instructions.merged(max_chars=max(800, max_chars // 3)))
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 40] + "\n[... context truncated ...]"
        return text


class ContextEngine:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    def collect(self, inv: "Investigation") -> ContextBundle:
        bundle = ContextBundle()
        task = inv.task
        cfg = self.h.config

        # repository ---------------------------------------------------
        repo = Path(task.workspace) if task.workspace else None
        if repo and repo.exists():
            info: dict[str, Any] = {"path": str(repo)}
            res = self.h.executor.run("git_current_branch", {"repo": str(repo)}, task, agent="context")
            if res.ok and isinstance(res.output, dict):
                info.update({"branch": res.output.get("branch"), "remote": res.output.get("remote")})
            st = self.h.executor.run("git_status", {"repo": str(repo)}, task, agent="context")
            if st.ok and isinstance(st.output, dict):
                info["clean"] = st.output.get("clean")
            log = self.h.executor.run("git_log", {"repo": str(repo), "n": 5}, task, agent="context")
            if log.ok and isinstance(log.output, dict):
                bundle.recent_changes = log.output.get("commits", [])
                if bundle.recent_changes:
                    info["commit"] = bundle.recent_changes[0].get("sha")
            bundle.repository = info
            task.links.repository = info.get("remote") or str(repo)
            task.links.branch = info.get("branch")
            bundle.instructions = discover(repo, root=repo)
        else:
            bundle.instructions = discover(cfg.project_root, root=cfg.project_root)
        if bundle.instructions and bundle.instructions.files:
            inv.notes.append(f"loaded instructions from {', '.join(bundle.instructions.paths)}")

        # environment --------------------------------------------------
        kube_context = None
        if "kubernetes" in inv.targets.get("domains", []) or inv.target("namespace"):
            res = self.h.executor.run("kubectl_current_context", {}, task, agent="context")
            if res.ok and isinstance(res.output, dict):
                kube_context = res.output.get("context")
                bundle.kubernetes["context"] = kube_context
        ns = inv.target("namespace") or cfg.default_namespace
        bundle.kubernetes["namespace"] = ns
        if inv.target("deployment"):
            bundle.kubernetes["deployment"] = inv.target("deployment")
        resolution = resolve_environment(cfg, kube_context=kube_context, namespace=ns, branch=bundle.repository.get("branch"),
                                         untrusted_hints=inv.targets.get("env_hints", []))
        bundle.environment = resolution
        task.environment = resolution.environment if resolution.environment != Environment.UNKNOWN else Environment.UNKNOWN
        task.context["environment_resolution"] = {"value": resolution.environment.value, "source": resolution.source, "evidence": resolution.evidence}
        inv.log.fact(f"Environment resolved as '{resolution.environment.value}' from {resolution.source}. " + " ".join(resolution.evidence),
                     source="environment-resolver", environment=resolution.environment.value)

        # jira ----------------------------------------------------------
        if inv.target("ticket"):
            res = self.h.executor.run("jira_get_issue", {"key": inv.target("ticket")}, task, agent="context")
            if res.ok and isinstance(res.output, dict):
                bundle.jira = res.output
                task.links.jira_issue = res.output.get("key")

        # memory + runbooks ----------------------------------------------
        bundle.memory = self.h.memory.context_summary(task.request)
        bundle.runbooks = [rb.name for rb in self.h.runbooks.find(task.request, limit=3)]
        task.context["bundle"] = bundle.to_dict()
        task.context["summary"] = bundle.summarize(cfg.limits.max_context_chars)
        return bundle
