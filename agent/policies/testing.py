"""Policy-as-Code test runner for policy verification suites."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models import AgentIdentity, Environment, OperatingMode, PermissionLevel, RiskLevel, ToolSpec
from agent.policies.engine import Policy, PolicyEngine


@dataclass
class PolicyTestCase:
    name: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    environment: str = "dev"
    mode: str = "approval"
    command: Optional[str] = None
    target_branch: Optional[str] = None
    identity: Optional[dict[str, Any]] = None
    expected_allowed: bool = True
    expected_requires_approval: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PolicyTestCase":
        return cls(
            name=d.get("name", "unnamed test case"),
            tool=d.get("tool", "unknown_tool"),
            arguments=dict(d.get("arguments", {})),
            environment=d.get("environment", "dev"),
            mode=d.get("mode", "approval"),
            command=d.get("command"),
            target_branch=d.get("target_branch"),
            identity=dict(d["identity"]) if d.get("identity") else None,
            expected_allowed=bool(d.get("expected_allowed", True)),
            expected_requires_approval=bool(d.get("expected_requires_approval", False)),
        )


@dataclass
class PolicyTestResult:
    name: str
    passed: bool
    detail: str
    decision: dict[str, Any]


class PolicyTestSuite:
    def __init__(self, policy: Policy) -> None:
        self.engine = PolicyEngine(policy)

    def run_case(self, test: PolicyTestCase, spec: Optional[ToolSpec] = None) -> PolicyTestResult:
        if spec is None:
            # Default fallback tool spec if tool catalog isn't passed
            spec = ToolSpec(
                name=test.tool,
                description=f"Mock spec for {test.tool}",
                requires_approval=test.expected_requires_approval,
                permission=PermissionLevel.MODIFY if "delete" in test.tool or "apply" in test.tool else PermissionLevel.READ,
                mutating="delete" in test.tool or "apply" in test.tool or "restart" in test.tool,
            )

        env = Environment.parse(test.environment)
        mode = OperatingMode.parse(test.mode)
        ident = AgentIdentity(**test.identity) if test.identity else None

        decision = self.engine.evaluate(
            spec,
            test.arguments,
            environment=env,
            mode=mode,
            command=test.command,
            target_branch=test.target_branch,
            identity=ident,
        )

        passed = (decision.allowed == test.expected_allowed) and (decision.requires_approval == test.expected_requires_approval)
        detail = (
            f"PASS"
            if passed
            else f"FAIL: expected allowed={test.expected_allowed}, requires_approval={test.expected_requires_approval}; "
                 f"got allowed={decision.allowed}, requires_approval={decision.requires_approval} (reason: {decision.reason})"
        )

        return PolicyTestResult(name=test.name, passed=passed, detail=detail, decision=decision.to_dict())

    def run_suite_file(self, suite_path: Path) -> list[PolicyTestResult]:
        if not suite_path.exists():
            raise FileNotFoundError(f"Test suite file not found: {suite_path}")
        data = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
        raw_cases = data.get("tests", [])
        results = []
        for c in raw_cases:
            tc = PolicyTestCase.from_dict(c)
            results.append(self.run_case(tc))
        return results
