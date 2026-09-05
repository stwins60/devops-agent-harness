"""Docker specialist: Dockerfile -> Build -> Image -> Container -> Process -> Network -> Volume."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from agent.models import Diagnosis, Hypothesis, Plan, ProposedChange, RiskLevel, PermissionLevel, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class DockerSpecialist(Specialist):
    name = "docker-agent"
    description = "Troubleshoots Dockerfiles, builds, images, containers, networks and volumes."
    domains = ["docker"]
    keywords = ["docker", "container", "containers", "dockerfile", "compose", "image", "images", "registry"]

    def investigate(self, inv: Investigation) -> None:
        self.use_runbook(inv, inv.task.request, domain="docker")
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        if repo and (repo / "Dockerfile").exists():
            text = (repo / "Dockerfile").read_text(encoding="utf-8", errors="replace")
            stages = len(re.findall(r"^FROM\s", text, re.M))
            exposed = re.findall(r"^EXPOSE\s+(\d+)", text, re.M)
            inv.log.fact(f"Dockerfile: {stages} stage(s), EXPOSE {exposed or 'none'}, base {re.search(r'^FROM\s+(\S+)', text, re.M).group(1) if stages else '?'}.",
                         source="Dockerfile", dockerfile_stages=stages, dockerfile_expose=exposed)
        res = self.call(inv, "docker_ps", {"all": True}, purpose="list containers")
        if not res.ok:
            if res.failure_kind in ("network", "unavailable"):
                inv.blocked = f"docker daemon unavailable: {res.error}"
            return
        containers = res.output.get("containers", [])
        target = inv.target("container")
        chosen = [c for c in containers if target and target in str(c.get("Names", ""))] or [c for c in containers if c.get("State") != "running"] or containers[:1]
        for c in chosen[:2]:
            name = str(c.get("Names", "")).lstrip("/")
            inv.log.fact(f"Container {name}: state {c.get('State')} ({c.get('Status')}), image {c.get('Image')}, ports {c.get('Ports')}.", source="docker_ps",
                         container=name, container_state=c.get("State"), container_image=c.get("Image"))
            insp = self.call(inv, "docker_inspect", {"container": name}, purpose="inspect container state")
            if insp.ok:
                st = insp.output.get("state", {})
                inv.log.fact(f"Container {name} inspect: status {st.get('Status')}, exit code {st.get('ExitCode')}, OOMKilled={st.get('OOMKilled')}, "
                             f"error '{st.get('Error')}', memory limit {insp.output.get('host_config', {}).get('Memory')}, exposed {insp.output.get('exposed_ports')}.",
                             source=f"docker_inspect({name})", exit_code=st.get("ExitCode"), oom_killed=st.get("OOMKilled"), memory_limit=insp.output.get("host_config", {}).get("Memory"))
            logs = self.call(inv, "docker_logs", {"container": name, "tail": 50}, purpose="container logs")
            if logs.ok:
                lines = [l for l in logs.output.get("lines", []) if l.strip()]
                errors = [l for l in lines if re.search(r"error|exception|traceback|fatal|panic|address already in use|permission denied|no such file", l, re.I)]
                inv.log.fact(f"Logs of {name} (last lines): " + " | ".join(lines[-5:]), source=f"docker_logs({name})", log_errors=errors[:5], log_lines=lines[-5:])
        if repo and (repo / "docker-compose.yml").exists() or (repo and (repo / "compose.yaml").exists()):
            comp = self.call(inv, "docker_compose_ps", {"path": str(repo)}, purpose="compose services")
            if comp.ok:
                inv.log.fact(f"Compose services: {[(s.get('Name') or s.get('Service'), s.get('State')) for s in comp.output.get('services', [])]}.", source="docker_compose_ps")

    def analyzers(self):
        return [("docker.oom", _oom), ("docker.exit_error", _exit_error), ("docker.port_conflict", _port_conflict), ("docker.healthy", _healthy)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        if not diagnosis.conclusion or "no fault" in diagnosis.conclusion.lower():
            return None
        plan = Plan(task_id=inv.task.id, title=f"Docker: {diagnosis.conclusion[:70]}", problem=diagnosis.problem, root_cause=diagnosis.conclusion,
                    evidence=[f.statement for f in diagnosis.facts][:10], risk_level=RiskLevel.LOW)
        name = inv.log.get("container")
        if "oom" in diagnosis.conclusion.lower():
            plan.changes.append(ProposedChange(description=f"Raise the memory limit for container {name} (compose `mem_limit` / `--memory`) or fix memory growth", kind="file",
                                               target="docker-compose.yml", tool=None, risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY, rollback="restore previous limit"))
        elif "port" in diagnosis.conclusion.lower():
            plan.changes.append(ProposedChange(description="Change the host port mapping or stop the process already bound to the port", kind="file", target="docker-compose.yml",
                                               tool=None, risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY, rollback="restore previous mapping"))
        else:
            plan.changes.append(ProposedChange(description=f"Fix the application error shown in the logs of {name}", kind="file", target="application code", tool=None,
                                               risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY, rollback="git revert"))
        plan.validation = ["docker build succeeds", "container stays running (docker ps) and logs show readiness"]
        plan.rollback = ["restore previous compose/Dockerfile from git"]
        return plan

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        results = []
        if repo and (repo / "Dockerfile").exists() and any(c.applied for c in plan.changes):
            res = self.call(inv, "docker_build", {"path": str(repo), "tag": f"devops-agent/{inv.task.id.lower()}:validate"}, purpose="validate the image still builds")
            results.append(ValidationResult("docker build", res.ok, (res.error or "image built")[:200], skipped=res.dry_run))
        return results


def _oom(log: EvidenceLog) -> list[Hypothesis]:
    if log.get("oom_killed"):
        log.recommendation("Increase the container memory limit or fix the memory growth; verify with docker stats.")
        return [Hypothesis(statement=f"Container {log.get('container')} was OOM-killed (limit {log.get('memory_limit')} bytes).", validation="State.OOMKilled=true in docker inspect.",
                           status="confirmed", confidence=0.95)]
    return []


def _exit_error(log: EvidenceLog) -> list[Hypothesis]:
    code = log.get("exit_code")
    if code not in (None, 0) and log.get("container_state") != "running":
        errors = log.get("log_errors") or []
        return [Hypothesis(statement=f"Container {log.get('container')} exited with code {code}: {errors[0][:120] if errors else 'no error captured in logs'}.",
                           validation="Error line in docker logs explaining the exit.", status="confirmed" if errors and code != 137 else "unvalidated", confidence=0.85 if errors else 0.5)]
    return []


def _port_conflict(log: EvidenceLog) -> list[Hypothesis]:
    if any("address already in use" in (l or "").lower() for l in (log.get("log_errors") or [])):
        log.recommendation("Free the host port or change the published port mapping.")
        return [Hypothesis(statement="Port conflict: the container cannot bind because the address is already in use.", validation="'address already in use' in logs.",
                           status="confirmed", confidence=0.9)]
    return []


def _healthy(log: EvidenceLog) -> list[Hypothesis]:
    if log.get("container_state") == "running" and log.get("exit_code") in (0, None) and not log.get("log_errors"):
        return [Hypothesis(statement=f"No fault detected: container {log.get('container')} is running without errors in recent logs.", validation="docker ps + logs.",
                           status="confirmed", confidence=0.8)]
    return []
