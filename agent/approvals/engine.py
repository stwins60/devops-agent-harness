"""Approval engine.

An approval handler is asked whenever the policy engine says a tool call
requires human consent. Handlers are pluggable: interactive terminal prompt,
non-interactive auto-deny, pre-approved allowlists (for CI / autonomous
execution of an already-approved plan) and a recording handler for tests.
"""
from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from agent.models import ApprovalDecision, ApprovalRecord, ApprovalRequest


@dataclass
class ApprovalOutcome:
    decision: ApprovalDecision
    decided_by: str
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.decision == ApprovalDecision.APPROVE

    def record(self, request: ApprovalRequest) -> ApprovalRecord:
        return ApprovalRecord(request=request.to_dict(), decision=self.decision.value, decided_by=self.decided_by, note=self.note)


class ApprovalHandler(Protocol):
    name: str

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome: ...


class AutoDenyHandler:
    """Used when no human is available (non-interactive) and nothing is pre-approved."""

    name = "auto-deny"

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        return ApprovalOutcome(ApprovalDecision.DENY, self.name, "non-interactive session: approval required but no approver available")


class AutoApproveHandler:
    """Approves everything except operations that demand explicit confirmation unless allow_explicit=True.

    Intended for local development and tests only; the policy engine still blocks forbidden operations.
    """

    name = "auto-approve"

    def __init__(self, allow_explicit: bool = False) -> None:
        self.allow_explicit = allow_explicit

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        if explicit and not self.allow_explicit:
            return ApprovalOutcome(ApprovalDecision.DENY, self.name, "explicit confirmation required; auto-approve refuses DESTROY/production operations")
        return ApprovalOutcome(ApprovalDecision.APPROVE, self.name, "auto-approved")


class AllowlistHandler:
    """Approves operations that match a pre-approved list (tool names or 'tool:target' globs)."""

    name = "allowlist"

    def __init__(self, allowed: list[str], fallback: Optional[ApprovalHandler] = None) -> None:
        self.allowed = list(allowed)
        self.fallback = fallback or AutoDenyHandler()

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        candidates = [req.tool or "", req.operation]
        for pat in self.allowed:
            if any(c and fnmatch.fnmatch(c, pat) for c in candidates):
                return ApprovalOutcome(ApprovalDecision.APPROVE, self.name, f"pre-approved by '{pat}'")
        return self.fallback.request(req, explicit=explicit)


class RecordingHandler:
    """Test helper: returns scripted decisions and records every request."""

    name = "recording"

    def __init__(self, decisions: Optional[list[ApprovalDecision]] = None, default: ApprovalDecision = ApprovalDecision.APPROVE) -> None:
        self.decisions = list(decisions or [])
        self.default = default
        self.requests: list[ApprovalRequest] = []

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        self.requests.append(req)
        decision = self.decisions.pop(0) if self.decisions else self.default
        return ApprovalOutcome(decision, self.name, "scripted")


class InteractiveHandler:
    """Terminal prompt supporting approve / deny / skip / show diff / show plan / rollback."""

    name = "interactive"

    HELP = "Approve? [y]es / [n]o / [s]kip / [d]iff / [p]lan / [r]ollback / [?]help: "

    def __init__(self, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = None) -> None:
        self.input_fn = input_fn
        self.output_fn = output_fn or (lambda s: print(s, file=sys.stdout))

    def request(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        self.output_fn("\n" + "=" * 72 + "\nAPPROVAL REQUIRED\n" + "=" * 72)
        self.output_fn(req.render())
        while True:
            try:
                answer = self.input_fn(self.HELP).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalOutcome(ApprovalDecision.DENY, self.name, "input closed")
            if answer in ("y", "yes", "approve"):
                if explicit:
                    confirm = self.input_fn(f"Type 'approve {req.operation.split()[0]}' to confirm this {req.risk.value.upper()} risk operation in {req.environment.upper()}: ").strip().lower()
                    if confirm != f"approve {req.operation.split()[0].lower()}":
                        self.output_fn("Confirmation did not match; operation denied.")
                        return ApprovalOutcome(ApprovalDecision.DENY, self.name, "explicit confirmation mismatch")
                return ApprovalOutcome(ApprovalDecision.APPROVE, self.name, "approved interactively")
            if answer in ("n", "no", "deny", ""):
                return ApprovalOutcome(ApprovalDecision.DENY, self.name, "denied interactively")
            if answer in ("s", "skip"):
                return ApprovalOutcome(ApprovalDecision.SKIP, self.name, "skipped interactively")
            if answer in ("d", "diff", "show diff"):
                self.output_fn(req.diff or "(no diff available for this operation)")
            elif answer in ("p", "plan", "show plan"):
                self.output_fn(req.plan or "(no plan available)")
            elif answer in ("r", "rollback"):
                self.output_fn(req.rollback or "(no automatic rollback available)")
            else:
                self.output_fn("y=approve  n=deny  s=skip this step  d=show diff  p=show plan  r=show rollback")


@dataclass
class ApprovalEngine:
    handler: ApprovalHandler
    records: list[ApprovalRecord] = field(default_factory=list)

    def ask(self, req: ApprovalRequest, *, explicit: bool = False) -> ApprovalOutcome:
        outcome = self.handler.request(req, explicit=explicit)
        self.records.append(outcome.record(req))
        return outcome


def build_handler(*, interactive: bool, auto_approve: bool = False, preapproved: Optional[list[str]] = None,
                  allow_explicit: bool = False) -> ApprovalHandler:
    if auto_approve:
        base: ApprovalHandler = AutoApproveHandler(allow_explicit=allow_explicit)
    elif interactive:
        base = InteractiveHandler()
    else:
        base = AutoDenyHandler()
    if preapproved:
        return AllowlistHandler(preapproved, fallback=base)
    return base
