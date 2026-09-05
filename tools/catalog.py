"""Build the tool registry and backend map for a configuration (real or mock)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent.config import HarnessConfig
from tools.adapters import CliTool
from tools.base import ToolError
from tools.mock.world import MockWorld
from tools.registry import ToolRegistry


def build_registry(config: HarnessConfig, extra_dirs: Optional[list[Path]] = None) -> ToolRegistry:
    from tools.ansible.tools import build_tools as ansible_tools
    from tools.aws.tools import build_tools as aws_tools
    from tools.cicd.tools import build_tools as cicd_tools
    from tools.docker.tools import build_tools as docker_tools
    from tools.filesystem.tools import build_tools as fs_tools
    from tools.git.tools import build_tools as git_tools
    from tools.github.tools import build_tools as github_tools
    from tools.gitlab.tools import build_tools as gitlab_tools
    from tools.jira.tools import build_tools as jira_tools
    from tools.kubernetes.tools import build_tools as k8s_tools
    from tools.linux.tools import build_tools as linux_tools
    from tools.networking.tools import build_tools as net_tools
    from tools.observability.tools import build_tools as obs_tools
    from tools.security.tools import build_tools as sec_tools
    from tools.terraform.tools import build_tools as tf_tools

    registry = ToolRegistry()
    for builder in (fs_tools, git_tools, github_tools, gitlab_tools, jira_tools, docker_tools, k8s_tools, linux_tools, net_tools, aws_tools,
                    tf_tools, ansible_tools, cicd_tools, obs_tools, sec_tools):
        registry.register_all(builder())
    registry.register(CliTool())
    return registry


class LazyBackend:
    """Defers construction of a real backend until first use so missing credentials surface as ToolError, not import-time crashes."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._instance: Any = None
        self._error: Optional[ToolError] = None

    def _get(self) -> Any:
        if self._instance is None and self._error is None:
            try:
                self._instance = self._factory()
            except ToolError as exc:
                self._error = exc
            except Exception as exc:  # pragma: no cover - defensive
                self._error = ToolError(str(exc), kind="unavailable")
        if self._error:
            raise self._error
        return self._instance

    def __getattr__(self, item: str) -> Any:
        return getattr(self._get(), item)


def build_backends(config: HarnessConfig, world: Optional[MockWorld] = None) -> dict[str, Any]:
    if config.mock:
        from tools.ansible.tools import MockAnsibleBackend
        from tools.aws.tools import MockAwsBackend
        from tools.docker.tools import MockDockerBackend
        from tools.git.tools import MockGitBackend
        from tools.github.tools import MockGitHubBackend
        from tools.gitlab.tools import MockGitLabBackend
        from tools.jira.tools import MockJiraBackend
        from tools.kubernetes.tools import MockKubernetesBackend
        from tools.linux.tools import MockLinuxBackend
        from tools.networking.tools import MockNetworkBackend
        from tools.observability.tools import MockObservabilityBackend
        from tools.security.tools import MockSecurityBackend
        from tools.terraform.tools import MockTerraformBackend

        world = world or MockWorld.build(config.mock_scenario)
        return {
            "world": world, "kubernetes": MockKubernetesBackend(world), "docker": MockDockerBackend(world), "git": MockGitBackend(world),
            "github": MockGitHubBackend(world), "gitlab": MockGitLabBackend(world), "jira": MockJiraBackend(world), "linux": MockLinuxBackend(world),
            "network": MockNetworkBackend(world), "aws": MockAwsBackend(world), "terraform": MockTerraformBackend(world),
            "ansible": MockAnsibleBackend(world), "observability": MockObservabilityBackend(world), "security": MockSecurityBackend(world),
        }

    from tools.ansible.tools import AnsibleCliBackend
    from tools.aws.tools import AwsCliBackend
    from tools.docker.tools import DockerCliBackend
    from tools.git.tools import GitCliBackend
    from tools.github.tools import GitHubRestBackend
    from tools.gitlab.tools import GitLabRestBackend
    from tools.jira.tools import JiraRestBackend
    from tools.kubernetes.tools import KubectlBackend
    from tools.linux.tools import LocalLinuxBackend
    from tools.networking.tools import SocketNetworkBackend
    from tools.observability.tools import HttpObservabilityBackend
    from tools.security.tools import CliSecurityBackend
    from tools.terraform.tools import TerraformCliBackend

    return {
        "kubernetes": KubectlBackend(context=config.kube_context), "docker": DockerCliBackend(), "git": GitCliBackend(),
        "github": LazyBackend(GitHubRestBackend), "gitlab": LazyBackend(GitLabRestBackend),
        "jira": LazyBackend(lambda: JiraRestBackend(config.jira_url)), "linux": LocalLinuxBackend(config.extra.get("linux_host")),
        "network": SocketNetworkBackend(), "aws": AwsCliBackend(profile=config.aws_profile, region=config.aws_region),
        "terraform": TerraformCliBackend(), "ansible": AnsibleCliBackend(),
        "observability": HttpObservabilityBackend(config.prometheus_url, config.loki_url, config.extra.get("alertmanager_url")),
        "security": CliSecurityBackend(),
    }
