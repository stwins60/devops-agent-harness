"""Policy engine: decides whether a tool call is allowed and whether it needs approval.

The engine is deterministic and runs outside the model. The model may *ask*
for a tool call; only this engine decides what happens with it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models import CommandClass, Environment, OperatingMode, PermissionLevel, RiskLevel, ToolSpec
from agent.policies.classifier import Classification, CommandClassifier, rules_from_config

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "default.yaml"

_PERM_ORDER = ["READ", "ANALYZE", "MODIFY", "DEPLOY", "DESTROY"]
WORKSPACE_CATEGORIES = {"filesystem", "git", "github", "gitlab", "jira"}


@dataclass
class EnvironmentPolicy:
    name: str
    auto_allow_max_permission: PermissionLevel = PermissionLevel.READ
    require_approval: list[str] = field(default_factory=lambda: ["*"])
    explicit_confirmation: bool = True

    def requires_approval_for(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatch(tool_name, pat) for pat in self.require_approval)


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str
    permission: PermissionLevel
    risk: RiskLevel
    explicit_confirmation: bool = False
    classification: Optional[Classification] = None
    environment: Environment = Environment.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "permission": self.permission.name,
            "risk": self.risk.value,
            "explicit_confirmation": self.explicit_confirmation,
            "command_class": self.classification.klass.value if self.classification else None,
            "environment": self.environment.value,
        }


class Policy:
    """Merged, immutable-by-the-model policy document."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.version = int(data.get("version", 1))
        self.environments: dict[str, EnvironmentPolicy] = {}
        for name, spec in (data.get("environments") or {}).items():
            spec = spec or {}
            self.environments[name] = EnvironmentPolicy(
                name=name,
                auto_allow_max_permission=PermissionLevel.parse(spec.get("auto_allow_max_permission", "READ")),
                require_approval=[str(x) for x in spec.get("require_approval", ["*"])],
                explicit_confirmation=bool(spec.get("explicit_confirmation", True)),
            )
        self.protected_branches: list[str] = [str(x) for x in data.get("protected_branches", [])]
        self.forbidden: list[str] = [str(x) for x in data.get("forbidden", [])]
        self.forbidden_tools: list[str] = [str(x) for x in data.get("forbidden_tools", [])]
        self.tool_overrides: dict[str, dict[str, Any]] = dict(data.get("tool_overrides") or {})
        self.command_rules = rules_from_config(data.get("commands") or [])
        self.limits: dict[str, int] = {k: int(v) for k, v in (data.get("limits") or {}).items()}

    def env(self, environment: Environment) -> EnvironmentPolicy:
        name = environment.value
        if name in self.environments:
            return self.environments[name]
        # unknown / unmapped environments fall back to the strictest definition
        return self.environments.get("unknown") or EnvironmentPolicy(name="unknown")

    def is_protected_branch(self, branch: str) -> bool:
        return any(fnmatch.fnmatch(branch, pat) for pat in self.protected_branches)

    def is_forbidden_tool(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatch(tool_name, pat) for pat in self.forbidden_tools)

    # -- merging ------------------------------------------------------------
    @staticmethod
    def merge_stricter(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Merge a project policy into the base policy allowing only stricter results."""
        merged = yaml.safe_load(yaml.safe_dump(base))  # deep copy
        for key, value in (override or {}).items():
            if key == "environments":
                for env_name, spec in (value or {}).items():
                    spec = spec or {}
                    current = merged.setdefault("environments", {}).setdefault(env_name, {
                        "auto_allow_max_permission": "READ", "require_approval": ["*"], "explicit_confirmation": True})
                    if "auto_allow_max_permission" in spec:
                        cur = _PERM_ORDER.index(str(current.get("auto_allow_max_permission", "READ")).upper())
                        new = _PERM_ORDER.index(str(spec["auto_allow_max_permission"]).upper())
                        current["auto_allow_max_permission"] = _PERM_ORDER[min(cur, new)]
                    if "require_approval" in spec:
                        current["require_approval"] = sorted(set(current.get("require_approval", [])) | set(spec["require_approval"]))
                    if spec.get("explicit_confirmation"):
                        current["explicit_confirmation"] = True
            elif key in ("protected_branches", "forbidden", "forbidden_tools", "commands"):
                existing = merged.get(key, []) or []
                for item in value or []:
                    if item not in existing:
                        existing.append(item)
                merged[key] = existing
            elif key == "tool_overrides":
                dest = merged.setdefault("tool_overrides", {})
                for tool, spec in (value or {}).items():
                    cur = dest.setdefault(tool, {})
                    if spec.get("requires_approval"):
                        cur["requires_approval"] = True
                    if "risk_level" in spec:
                        order = ["low", "medium", "high", "critical"]
                        cur_rank = order.index(str(cur.get("risk_level", "low")).lower())
                        cur["risk_level"] = order[max(cur_rank, order.index(str(spec["risk_level"]).lower()))]
                    if spec.get("disabled"):
                        cur["disabled"] = True
            elif key == "limits":
                dest = merged.setdefault("limits", {})
                for k, v in (value or {}).items():
                    dest[k] = min(int(dest.get(k, v)), int(v))
            elif key == "version":
                merged["version"] = value
            # any other key from a project policy is ignored: it cannot relax the base policy
        return merged


def load_policy(project_root: Optional[Path] = None, extra_paths: Optional[list[Path]] = None) -> Policy:
    base = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8")) or {}
    paths: list[Path] = []
    if project_root:
        paths.append(Path(project_root) / ".agent" / "policy.yaml")
        paths.append(Path(project_root) / "policies" / "project.yaml")
    paths.extend(extra_paths or [])
    merged = base
    for p in paths:
        if p.exists() and p.resolve() != DEFAULT_POLICY_PATH.resolve():
            override = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(override, dict):
                raise ValueError(f"Policy file {p} must be a mapping")
            merged = Policy.merge_stricter(merged, override)
    return Policy(merged)


class PolicyEngine:
    def __init__(self, policy: Policy, classifier: Optional[CommandClassifier] = None) -> None:
        self.policy = policy
        self.classifier = classifier or CommandClassifier(extra_rules=policy.command_rules)

    # ------------------------------------------------------------------
    def evaluate(self, spec: ToolSpec, args: dict[str, Any], *, environment: Environment, mode: OperatingMode,
                 command: Optional[str] = None, target_branch: Optional[str] = None) -> PolicyDecision:
        # Workspace tools (files, git, PRs, tickets) act on the repository/tracker, not on a running
        # environment, so the environment-specific auto-allow/explicit rules do not apply to them.
        # Their own ToolSpec (requires_approval, permission) and the operating mode still do.
        workspace_tool = spec.category in WORKSPACE_CATEGORIES and command is None
        env_policy = self.policy.env(Environment.LOCAL if workspace_tool else environment)
        classification: Optional[Classification] = None
        permission = spec.permission
        risk = spec.risk_level
        requires_approval = spec.requires_approval
        mutating = spec.mutating or permission >= PermissionLevel.MODIFY

        # 1. hard blocks ------------------------------------------------
        if self.policy.is_forbidden_tool(spec.name):
            return PolicyDecision(False, False, f"tool '{spec.name}' is forbidden by policy", permission, risk,
                                  environment=environment)
        override = self.policy.tool_overrides.get(spec.name, {})
        if override.get("disabled"):
            return PolicyDecision(False, False, f"tool '{spec.name}' is disabled by project policy", permission, risk,
                                  environment=environment)
        if override.get("requires_approval"):
            requires_approval = True
        if "risk_level" in override:
            risk = max(risk, RiskLevel.parse(override["risk_level"]), key=lambda r: r.rank)

        if command is not None:
            classification = self.classifier.classify(command)
            if classification.forbidden:
                return PolicyDecision(False, False, f"command forbidden: {classification.reason}", PermissionLevel.DESTROY,
                                      RiskLevel.CRITICAL, classification=classification, environment=environment)
            permission = max(permission, classification.permission)
            if classification.klass == CommandClass.DANGEROUS:
                risk = max(risk, RiskLevel.HIGH, key=lambda r: r.rank)
                requires_approval = True
                mutating = True
            elif classification.klass == CommandClass.CAUTION:
                risk = max(risk, RiskLevel.MEDIUM, key=lambda r: r.rank)
                mutating = True

        if target_branch and self.policy.is_protected_branch(target_branch) and spec.name.startswith(("git_push", "git_merge")):
            return PolicyDecision(False, False,
                                  f"direct push/merge to protected branch '{target_branch}' is not permitted; use a feature branch and a pull request",
                                  PermissionLevel.DEPLOY, RiskLevel.HIGH, classification=classification, environment=environment)

        # 2. operating mode gates ---------------------------------------
        if mode in (OperatingMode.READ_ONLY, OperatingMode.PLAN):
            if mutating or permission >= PermissionLevel.MODIFY:
                return PolicyDecision(False, False, f"{mode.value} mode does not permit mutating tools ({permission.name})",
                                      permission, risk, classification=classification, environment=environment)
            return PolicyDecision(True, False, "read-only operation", permission, risk, classification=classification,
                                  environment=environment)

        # 3. approval requirements --------------------------------------
        reasons: list[str] = []
        if requires_approval:
            reasons.append("tool requires approval")
        if permission >= PermissionLevel.DESTROY:
            reasons.append("DESTROY permission always requires approval")
        if mutating and env_policy.requires_approval_for(spec.name):
            reasons.append(f"{environment.value} policy requires approval for {spec.name}")
        if mutating and permission > env_policy.auto_allow_max_permission:
            reasons.append(f"{permission.name} exceeds auto-allowed {env_policy.auto_allow_max_permission.name} in {environment.value}")
        if mode == OperatingMode.APPROVAL and mutating:
            reasons.append("approval mode: all mutations require approval")
        if risk.rank >= RiskLevel.HIGH.rank and mutating:
            reasons.append(f"{risk.value} risk")

        needs = bool(reasons)
        explicit = needs and (env_policy.explicit_confirmation or permission >= PermissionLevel.DESTROY)
        return PolicyDecision(True, needs, "; ".join(reasons) if reasons else "auto-allowed by policy", permission, risk,
                              explicit_confirmation=explicit, classification=classification, environment=environment)

    def classify(self, command: str) -> Classification:
        return self.classifier.classify(command)
