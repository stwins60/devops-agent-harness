"""Docker tools: docker CLI backend + mock backend."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command


class DockerBackend(Protocol):
    def ps(self, all_containers: bool = True) -> list[dict[str, Any]]: ...
    def logs(self, container: str, tail: int = 200) -> str: ...
    def inspect(self, container: str) -> dict[str, Any]: ...
    def images(self) -> list[dict[str, Any]]: ...
    def build(self, path: str, tag: str, dockerfile: Optional[str] = None) -> str: ...
    def compose_ps(self, path: str) -> list[dict[str, Any]]: ...
    def restart(self, container: str) -> str: ...


class DockerCliBackend:
    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout

    def _run(self, *args: str, timeout: Optional[int] = None, cwd: Optional[Path] = None) -> str:
        out = run_command(["docker", *args], timeout=timeout or self.timeout, cwd=cwd, env_passthrough=("DOCKER_HOST",))
        if not out.ok:
            msg = (out.stderr or out.stdout).strip()
            kind = "network" if "cannot connect to the docker daemon" in msg.lower() else "not_found" if "no such" in msg.lower() else \
                   "permission" if "permission denied" in msg.lower() else "timeout" if out.timed_out else "unknown"
            raise ToolError(f"docker {args[0]} failed: {msg[:500]}", kind=kind)
        return out.stdout

    def _json_lines(self, *args: str) -> list[dict[str, Any]]:
        out = self._run(*args, "--format", "{{json .}}")
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def ps(self, all_containers: bool = True) -> list[dict[str, Any]]:
        return self._json_lines("ps", *(["-a"] if all_containers else []))

    def logs(self, container: str, tail: int = 200) -> str:
        out = run_command(["docker", "logs", "--tail", str(tail), container], timeout=self.timeout, env_passthrough=("DOCKER_HOST",))
        if not out.ok:
            raise ToolError(f"docker logs failed: {out.stderr[:400]}", kind="not_found" if "no such" in out.stderr.lower() else "unknown")
        return out.stdout + out.stderr

    def inspect(self, container: str) -> dict[str, Any]:
        data = json.loads(self._run("inspect", container))
        return data[0] if isinstance(data, list) and data else {}

    def images(self) -> list[dict[str, Any]]:
        return self._json_lines("images")

    def build(self, path: str, tag: str, dockerfile: Optional[str] = None) -> str:
        args = ["build", "-t", tag] + (["-f", dockerfile] if dockerfile else []) + [path]
        return self._run(*args, timeout=900)

    def compose_ps(self, path: str) -> list[dict[str, Any]]:
        out = self._run("compose", "ps", "--format", "json", cwd=Path(path))
        try:
            data = json.loads(out)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]

    def restart(self, container: str) -> str:
        return self._run("restart", container, timeout=120)


class MockDockerBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def _check(self) -> None:
        if self.world.flags.get("docker_unavailable"):
            raise ToolError("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", kind="network")

    def ps(self, all_containers: bool = True) -> list[dict[str, Any]]:
        self._check()
        return [c for c in self.world.docker["containers"] if all_containers or c["State"] == "running"]

    def logs(self, container: str, tail: int = 200) -> str:
        self._check()
        if container not in self.world.docker["logs"]:
            raise ToolError(f"Error: No such container: {container}", kind="not_found")
        return "\n".join(self.world.docker["logs"][container].splitlines()[-tail:])

    def inspect(self, container: str) -> dict[str, Any]:
        self._check()
        try:
            return self.world.docker["inspect"][container]
        except KeyError as exc:
            raise ToolError(f"Error: No such object: {container}", kind="not_found") from exc

    def images(self) -> list[dict[str, Any]]:
        self._check()
        return list(self.world.docker["images"])

    def build(self, path: str, tag: str, dockerfile: Optional[str] = None) -> str:
        self._check()
        if not self.world.docker.get("build_ok", True):
            raise ToolError("ERROR: failed to solve: process \"/bin/sh -c pip install -r requirements.txt\" did not complete successfully: exit code 1", kind="invalid")
        self.world.record("docker_build", tag=tag, path=path)
        self.world.docker["images"].append({"Repository": tag.split(":")[0], "Tag": tag.split(":")[-1] if ":" in tag else "latest", "Size": "142MB", "Id": "sha256:new"})
        return self.world.docker.get("build_log", "Successfully built\n")

    def compose_ps(self, path: str) -> list[dict[str, Any]]:
        self._check()
        return [{"Name": c["Names"], "State": c["State"], "Service": c["Names"].split("-")[-1]} for c in self.world.docker["containers"]]

    def restart(self, container: str) -> str:
        self._check()
        self.world.record("docker_restart", container=container)
        return container


@tool("docker_ps", "List containers (running and exited) with status and exit codes.", category="docker", permissions=["docker.read"],
      input_schema={"type": "object", "properties": {"all": {"type": "boolean"}}})
def docker_ps(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"containers": ctx.backend("docker").ps(bool(args.get("all", True)))}


@tool("docker_logs", "Fetch logs of a container.", category="docker", permissions=["docker.read"],
      input_schema={"type": "object", "properties": {"container": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["container"]})
def docker_logs(args: dict[str, Any], ctx: ToolContext) -> Any:
    text = ctx.backend("docker").logs(args["container"], int(args.get("tail") or 200))
    return {"container": args["container"], "text": text, "lines": text.splitlines()}


@tool("docker_inspect", "Inspect a container or image (state, exit code, OOMKilled, resources, env names).", category="docker", permissions=["docker.read"],
      input_schema={"type": "object", "properties": {"container": {"type": "string"}}, "required": ["container"]})
def docker_inspect(args: dict[str, Any], ctx: ToolContext) -> Any:
    data = ctx.backend("docker").inspect(args["container"])
    state = data.get("State", {})
    return {"state": state, "host_config": {k: v for k, v in data.get("HostConfig", {}).items() if k in ("Memory", "NanoCpus", "RestartPolicy")},
            "exposed_ports": list((data.get("Config", {}) or {}).get("ExposedPorts", {}).keys()),
            "env_names": [e.split("=", 1)[0] for e in (data.get("Config", {}) or {}).get("Env", [])]}


@tool("docker_images", "List local images.", category="docker", permissions=["docker.read"])
def docker_images(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"images": ctx.backend("docker").images()}


@tool("docker_compose_ps", "List docker compose services and their state.", category="docker", permissions=["docker.read"],
      input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
def docker_compose_ps(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"services": ctx.backend("docker").compose_ps(args.get("path") or str(ctx.workspace or "."))}


class DockerBuildTool(Tool):
    spec = ToolSpec(name="docker_build", description="Build an image from a Dockerfile (validation step; produces a local image only).",
                    risk_level=RiskLevel.MEDIUM, permission=PermissionLevel.MODIFY, permissions=["docker.build"], timeout=900, category="docker",
                    mutating=True, rollback="docker rmi {tag}",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "tag": {"type": "string"}, "dockerfile": {"type": "string"}}, "required": ["tag"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = ctx.backend("docker").build(args.get("path") or str(ctx.workspace or "."), args["tag"], args.get("dockerfile"))
        return ToolResult(ok=True, output={"tag": args["tag"], "log_tail": out[-2000:]}, tool=self.name, args=args)


class DockerRestartTool(Tool):
    spec = ToolSpec(name="docker_restart", description="Restart a container.", risk_level=RiskLevel.MEDIUM, requires_approval=True,
                    permission=PermissionLevel.DEPLOY, permissions=["docker.write"], timeout=120, category="docker", mutating=True,
                    rollback="not reversible (restart); previous container state is lost",
                    input_schema={"type": "object", "properties": {"container": {"type": "string"}}, "required": ["container"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output={"restarted": ctx.backend("docker").restart(args["container"])}, tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [docker_ps, docker_logs, docker_inspect, docker_images, docker_compose_ps, DockerBuildTool(), DockerRestartTool()]
