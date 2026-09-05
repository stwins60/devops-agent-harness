"""Runbook loader and matcher.

Runbooks are YAML files under ``runbooks/<domain>/`` (built-in) and
``.agent/runbooks/`` (project). Each runbook has a structured schema:

    name, description, trigger, severity, prechecks, diagnosis, commands,
    expected_results, remediation, validation, rollback, approval_required

The agent consults matching runbooks *before* inventing a procedure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

BUILTIN_RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"
REQUIRED_FIELDS = ("name", "description", "trigger", "severity", "diagnosis", "remediation", "validation", "rollback", "approval_required")


class RunbookError(Exception):
    pass


@dataclass
class RunbookStep:
    description: str
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    command: Optional[str] = None
    expected: Optional[str] = None
    approval_required: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "RunbookStep":
        if isinstance(raw, str):
            return cls(description=raw, command=raw if raw.split(" ")[0] in _CLI_PROGRAMS else None)
        if isinstance(raw, dict):
            return cls(description=str(raw.get("description") or raw.get("step") or raw.get("command") or raw.get("tool") or ""),
                       tool=raw.get("tool"), args=dict(raw.get("args") or {}), command=raw.get("command"),
                       expected=raw.get("expected"), approval_required=bool(raw.get("approval_required", False)))
        raise RunbookError(f"invalid runbook step: {raw!r}")


_CLI_PROGRAMS = {"kubectl", "docker", "aws", "terraform", "ansible", "ansible-playbook", "git", "helm", "systemctl", "journalctl",
                 "dig", "curl", "ss", "df", "free", "ps", "top", "ip", "psql", "mysql", "gh", "glab", "argocd", "flux"}


@dataclass
class Runbook:
    name: str
    description: str
    trigger: list[str]
    severity: str
    domain: str
    path: Path
    prechecks: list[RunbookStep] = field(default_factory=list)
    diagnosis: list[RunbookStep] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    expected_results: list[str] = field(default_factory=list)
    remediation: list[RunbookStep] = field(default_factory=list)
    validation: list[RunbookStep] = field(default_factory=list)
    rollback: list[RunbookStep] = field(default_factory=list)
    approval_required: bool = True
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def matches(self, text: str) -> int:
        """Score how well the runbook triggers match free text (0 = no match)."""
        low = text.lower()
        score = 0
        for trig in self.trigger:
            t = trig.lower().strip()
            if not t:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low):
                score += 3 + len(t.split())
            else:
                words = [w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2]
                hits = sum(1 for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low))
                if words and hits == len(words):
                    score += 2
                elif hits and hits >= max(1, len(words) - 1):
                    score += 1
        for tag in self.tags:
            if tag.lower() in low:
                score += 1
        return score

    def render(self) -> str:
        def steps(items: list[RunbookStep]) -> str:
            return "\n".join(f"  - {s.description}" + (f"  [tool: {s.tool}]" if s.tool else "") for s in items) or "  (none)"

        return "\n".join([
            f"Runbook: {self.name} ({self.domain}, severity {self.severity})",
            f"Description: {self.description}",
            f"Triggers: {', '.join(self.trigger)}",
            f"Approval required: {'yes' if self.approval_required else 'no'}",
            "Prechecks:", steps(self.prechecks),
            "Diagnosis:", steps(self.diagnosis),
            "Expected results:", "\n".join(f"  - {e}" for e in self.expected_results) or "  (none)",
            "Remediation:", steps(self.remediation),
            "Validation:", steps(self.validation),
            "Rollback:", steps(self.rollback),
        ])


def parse_runbook(path: Path, domain: Optional[str] = None) -> Runbook:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RunbookError(f"{path}: runbook must be a mapping")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise RunbookError(f"{path}: missing required fields {missing}")
    trig = data["trigger"]
    triggers = [str(t) for t in (trig if isinstance(trig, list) else [trig])]
    steps = lambda key: [RunbookStep.parse(s) for s in (data.get(key) or [])]  # noqa: E731
    return Runbook(
        name=str(data["name"]), description=str(data["description"]), trigger=triggers, severity=str(data["severity"]),
        domain=domain or path.parent.name, path=Path(path), prechecks=steps("prechecks"), diagnosis=steps("diagnosis"),
        commands=[str(c) for c in (data.get("commands") or [])],
        expected_results=[str(e) for e in (data.get("expected_results") or [])],
        remediation=steps("remediation"), validation=steps("validation"), rollback=steps("rollback"),
        approval_required=bool(data.get("approval_required", True)), tags=[str(t) for t in (data.get("tags") or [])], raw=data,
    )


class RunbookLibrary:
    def __init__(self, dirs: Optional[Iterable[Path]] = None) -> None:
        self.dirs = [Path(d) for d in (dirs or [BUILTIN_RUNBOOK_DIR])]
        self.runbooks: list[Runbook] = []
        self.errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        self.runbooks = []
        self.errors = []
        for d in self.dirs:
            if not d.exists():
                continue
            for path in sorted(d.rglob("*.y*ml")):
                try:
                    domain = path.parent.name if path.parent != d else "general"
                    self.runbooks.append(parse_runbook(path, domain))
                except Exception as exc:
                    self.errors.append(f"{path}: {exc}")

    def get(self, name: str) -> Optional[Runbook]:
        for rb in self.runbooks:
            if rb.name == name:
                return rb
        return None

    def find(self, text: str, *, domain: Optional[str] = None, limit: int = 3) -> list[Runbook]:
        scored = []
        for rb in self.runbooks:
            if domain and rb.domain != domain:
                continue
            s = rb.matches(text)
            if s > 0:
                scored.append((s, rb))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [rb for _, rb in scored[:limit]]

    def by_domain(self, domain: str) -> list[Runbook]:
        return [rb for rb in self.runbooks if rb.domain == domain]
