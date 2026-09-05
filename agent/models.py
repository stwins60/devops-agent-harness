"""Core data model shared by every layer of the harness.

Everything here is plain dataclasses / enums so the harness has no dependency
on a particular model provider, HTTP client or validation library.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class PermissionLevel(enum.IntEnum):
    """Ordered permission levels. Higher values imply more blast radius."""

    READ = 0
    ANALYZE = 1
    MODIFY = 2
    DEPLOY = 3
    DESTROY = 4

    @classmethod
    def parse(cls, value: "str | int | PermissionLevel") -> "PermissionLevel":
        if isinstance(value, PermissionLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper()]


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["low", "medium", "high", "critical"].index(self.value)

    @classmethod
    def parse(cls, value: "str | RiskLevel") -> "RiskLevel":
        if isinstance(value, RiskLevel):
            return value
        return cls(str(value).strip().lower())


class CommandClass(str, enum.Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


class Environment(str, enum.Enum):
    LOCAL = "local"
    DEV = "dev"
    QA = "qa"
    STAGING = "staging"
    PRODUCTION = "production"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: "str | Environment | None") -> "Environment":
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, Environment):
            return value
        v = str(value).strip().lower()
        aliases = {"prod": "production", "prd": "production", "stage": "staging", "stg": "staging",
                   "development": "dev", "test": "qa", "uat": "qa"}
        v = aliases.get(v, v)
        try:
            return cls(v)
        except ValueError:
            return cls.UNKNOWN

    @property
    def strictness(self) -> int:
        """Higher = stricter. UNKNOWN is treated as production for safety."""
        order = {"local": 0, "dev": 1, "qa": 2, "staging": 3, "production": 4, "unknown": 4}
        return order[self.value]


class OperatingMode(str, enum.Enum):
    READ_ONLY = "read-only"
    PLAN = "plan"
    APPROVAL = "approval"
    AUTONOMOUS = "autonomous"

    @classmethod
    def parse(cls, value: "str | OperatingMode") -> "OperatingMode":
        if isinstance(value, OperatingMode):
            return value
        v = str(value).strip().lower().replace("_", "-")
        aliases = {"readonly": "read-only", "ro": "read-only", "auto": "autonomous", "approve": "approval"}
        return cls(aliases.get(v, v))


class TaskStage(str, enum.Enum):
    """The lifecycle stages of a task, in order."""

    RECEIVED = "received"
    UNDERSTANDING = "task_understanding"
    CONTEXT = "context_discovery"
    INSPECTION = "inspection"
    RCA = "root_cause_analysis"
    PLAN = "plan"
    RISK = "risk_assessment"
    APPROVAL = "approval_gate"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    DOCUMENTATION = "documentation"
    UPDATE = "external_update"
    REPORT = "final_report"
    DONE = "done"

    @classmethod
    def ordered(cls) -> list["TaskStage"]:
        return list(cls)

    def next(self) -> "TaskStage":
        stages = TaskStage.ordered()
        idx = stages.index(self)
        return stages[min(idx + 1, len(stages) - 1)]

    def __lt__(self, other: "TaskStage") -> bool:  # type: ignore[override]
        stages = TaskStage.ordered()
        return stages.index(self) < stages.index(other)

    def __ge__(self, other: "TaskStage") -> bool:  # type: ignore[override]
        return not self.__lt__(other)


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class TaskKind(str, enum.Enum):
    QUESTION = "question"
    DIAGNOSE = "diagnose"
    JIRA = "jira"
    INCIDENT = "incident"
    PLAN = "plan"
    EXECUTE = "execute"
    FIX = "fix"


class EvidenceKind(str, enum.Enum):
    FACT = "FACT"
    HYPOTHESIS = "HYPOTHESIS"
    INFERENCE = "INFERENCE"
    RECOMMENDATION = "RECOMMENDATION"


class ApprovalDecision(str, enum.Enum):
    APPROVE = "approve"
    DENY = "deny"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Tool model
# ---------------------------------------------------------------------------
@dataclass
class ToolSpec:
    """Formal description of a tool. Mirrors the YAML manifest format."""

    name: str
    description: str
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    permission: PermissionLevel = PermissionLevel.READ
    permissions: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    timeout: int = 60
    rollback: Optional[str] = None
    category: str = "general"
    mutating: bool = False
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        d["permission"] = self.permission.name
        return d


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: Optional[str] = None
    duration: float = 0.0
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    skipped: bool = False
    failure_kind: Optional[str] = None  # auth|permission|network|rate_limit|timeout|not_found|invalid|unknown
    advice: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def text(self) -> str:
        if isinstance(self.output, str):
            return self.output
        if self.output is None:
            return self.error or ""
        import json

        try:
            return json.dumps(self.output, indent=2, default=str)
        except Exception:  # pragma: no cover - defensive
            return str(self.output)


# ---------------------------------------------------------------------------
# Evidence & RCA
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    kind: EvidenceKind
    statement: str
    source: str = ""  # e.g. "kubectl_get_pods(namespace=production)"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(kind=EvidenceKind(d["kind"]), statement=d["statement"], source=d.get("source", ""),
                   data=d.get("data", {}), timestamp=d.get("timestamp", now_iso()),
                   confidence=d.get("confidence", 1.0))


@dataclass
class Hypothesis:
    statement: str
    validation: str
    status: str = "unvalidated"  # unvalidated|confirmed|rejected|inconclusive
    confidence: float = 0.5
    supporting: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hypothesis":
        return cls(**d)


@dataclass
class Diagnosis:
    problem: str
    facts: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    conclusion: Optional[str] = None
    confidence: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    specialist: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem": self.problem,
            "facts": [f.to_dict() for f in self.facts],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "conclusion": self.conclusion,
            "confidence": self.confidence,
            "recommendations": list(self.recommendations),
            "specialist": self.specialist,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Diagnosis":
        return cls(problem=d.get("problem", ""),
                   facts=[Evidence.from_dict(x) for x in d.get("facts", [])],
                   hypotheses=[Hypothesis.from_dict(x) for x in d.get("hypotheses", [])],
                   conclusion=d.get("conclusion"), confidence=d.get("confidence", 0.0),
                   recommendations=d.get("recommendations", []), specialist=d.get("specialist", ""))


# ---------------------------------------------------------------------------
# Plan / change model
# ---------------------------------------------------------------------------
@dataclass
class ProposedChange:
    """A single unit of change the agent intends to make."""

    description: str
    kind: str  # file|command|infrastructure|external
    target: str  # file path, resource name, command
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    diff: Optional[str] = None
    risk: RiskLevel = RiskLevel.LOW
    permission: PermissionLevel = PermissionLevel.MODIFY
    rollback: Optional[str] = None
    environment: Optional[str] = None
    applied: bool = False
    result: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk"] = self.risk.value
        d["permission"] = self.permission.name
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProposedChange":
        d = dict(d)
        d["risk"] = RiskLevel.parse(d.get("risk", "low"))
        d["permission"] = PermissionLevel.parse(d.get("permission", "MODIFY"))
        return cls(**d)


@dataclass
class Plan:
    task_id: str
    title: str
    problem: str = ""
    root_cause: str = ""
    evidence: list[str] = field(default_factory=list)
    changes: list[ProposedChange] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    infrastructure: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    rollback: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    cost_notes: list[str] = field(default_factory=list)
    approved: bool = False
    approval_note: str = ""
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        d["changes"] = [c.to_dict() for c in self.changes]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        d = dict(d)
        d["risk_level"] = RiskLevel.parse(d.get("risk_level", "low"))
        d["changes"] = [ProposedChange.from_dict(c) for c in d.get("changes", [])]
        return cls(**d)


@dataclass
class ValidationResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def label(self) -> str:
        if self.skipped:
            return "SKIPPED"
        return "PASS" if self.passed else "FAIL"


@dataclass
class ApprovalRequest:
    operation: str
    description: str
    environment: str
    risk: RiskLevel
    resources: list[str] = field(default_factory=list)
    expected_impact: str = ""
    rollback: str = ""
    diff: Optional[str] = None
    plan: Optional[str] = None
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    cost_note: Optional[str] = None

    def render(self) -> str:
        lines = [
            f"The following operation will run in {self.environment.upper()}:",
            "",
            f"  {self.operation}",
            "",
            f"Description:\n  {self.description}",
        ]
        if self.resources:
            lines += ["", "Resources:"] + [f"  {r}" for r in self.resources]
        lines += ["", f"Risk:\n  {self.risk.value.upper()}"]
        if self.expected_impact:
            lines += ["", f"Expected impact:\n  {self.expected_impact}"]
        if self.cost_note:
            lines += ["", f"Cost:\n  {self.cost_note}"]
        rollback = self.rollback or "NOT AVAILABLE - this change cannot be rolled back automatically"
        lines += ["", f"Rollback:\n  {rollback}"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk"] = self.risk.value
        return d


@dataclass
class ApprovalRecord:
    request: dict[str, Any]
    decision: str
    decided_by: str
    timestamp: str = field(default_factory=now_iso)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Links:
    """Jira <-> Git <-> CI/CD <-> Deployment traceability."""

    jira_issue: Optional[str] = None
    repository: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    pull_request: Optional[str] = None
    pipeline: Optional[str] = None
    deployment: Optional[str] = None
    incident: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def chain(self) -> list[str]:
        items = [
            ("Jira", self.jira_issue), ("Repository", self.repository), ("Branch", self.branch),
            ("Commit", self.commit), ("Pull Request", self.pull_request), ("Pipeline", self.pipeline),
            ("Deployment", self.deployment), ("Incident", self.incident),
        ]
        return [f"{k}: {v}" for k, v in items if v]
