"""Fine-grained Role-Based Access Control (RBAC) and target scoping engine."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.models import AgentIdentity, PermissionLevel, ToolSpec


@dataclass
class RBACRole:
    name: str
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    denied_tools: list[str] = field(default_factory=list)
    max_permission: PermissionLevel = PermissionLevel.DESTROY
    allowed_namespaces: list[str] = field(default_factory=lambda: ["*"])
    allowed_aws_accounts: list[str] = field(default_factory=lambda: ["*"])


class RBACEvaluator:
    def __init__(self, roles: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.roles: dict[str, RBACRole] = {}
        if roles:
            for name, spec in roles.items():
                self.roles[name] = RBACRole(
                    name=name,
                    allowed_tools=[str(x) for x in spec.get("allowed_tools", ["*"])],
                    denied_tools=[str(x) for x in spec.get("denied_tools", [])],
                    max_permission=PermissionLevel.parse(spec.get("max_permission", "DESTROY")),
                    allowed_namespaces=[str(x) for x in spec.get("allowed_namespaces", ["*"])],
                    allowed_aws_accounts=[str(x) for x in spec.get("allowed_aws_accounts", ["*"])],
                )

    def is_tool_allowed(self, identity: AgentIdentity, spec: ToolSpec, args: dict[str, Any]) -> tuple[bool, str]:
        if not identity.roles:
            return True, "no specific RBAC restrictions apply"

        # If matching any role in identity
        role_objs = [self.roles[r] for r in identity.roles if r in self.roles]
        if not role_objs:
            # Default permissive when role definitions are unmapped
            return True, "role unmapped in policy"

        # Check permission level cap across roles
        max_perm = max(r.max_permission for r in role_objs)
        if spec.permission > max_perm:
            return False, f"permission {spec.permission.name} exceeds max allowed {max_perm.name} for roles {identity.roles}"

        # Check explicit tool denials
        for r in role_objs:
            for denied_pat in r.denied_tools:
                if fnmatch.fnmatch(spec.name, denied_pat):
                    return False, f"tool '{spec.name}' is explicitly denied by role '{r.name}'"

        # Check tool allowances
        tool_allowed = False
        for r in role_objs:
            if any(fnmatch.fnmatch(spec.name, pat) for pat in r.allowed_tools):
                tool_allowed = True
                break
        if not tool_allowed:
            return False, f"tool '{spec.name}' is not in allowed_tools for roles {identity.roles}"

        # Check target namespace scoping if provided in args
        target_ns = args.get("namespace")
        if target_ns:
            ns_allowed = False
            for r in role_objs:
                if any(fnmatch.fnmatch(target_ns, pat) for pat in r.allowed_namespaces):
                    ns_allowed = True
                    break
            if not ns_allowed:
                return False, f"namespace '{target_ns}' is not allowed for roles {identity.roles}"

        # Check target AWS account scoping if provided in args
        target_account = args.get("aws_account") or args.get("account_id")
        if target_account:
            acc_allowed = False
            for r in role_objs:
                if any(fnmatch.fnmatch(target_account, pat) for pat in r.allowed_aws_accounts):
                    acc_allowed = True
                    break
            if not acc_allowed:
                return False, f"AWS account '{target_account}' is not allowed for roles {identity.roles}"

        return True, "allowed by RBAC"
