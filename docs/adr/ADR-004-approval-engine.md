# ADR-004: Approval engine

## Context

Some operations must pause for a human: production changes, DESTROY-class actions, anything the
policy marks. The harness runs interactively (terminal), non-interactively (CI, MCP server) and
in tests, so approvals must be pluggable and every decision must be durable.

## Decision

* The policy engine produces a decision (`allowed`, `requires_approval`, `explicit_confirmation`);
  the executor turns it into an `ApprovalRequest` (operation, environment, risk, resources,
  expected impact, rollback, diff, plan) and asks the configured handler.
* Handlers: interactive terminal (approve / deny / skip / show diff / show plan / show rollback,
  typed confirmation for explicit cases), auto-deny (non-interactive), auto-approve (`--yes`,
  never explicit unless `--approve-all`), allowlist (pre-approved tools/targets), recording (tests).
* A plan-level gate runs once before implementation; per-tool gates still apply to each mutation.
* When no approver is available the task pauses in a resumable state instead of failing;
  `devops-agent execute TASK-ID` continues from the gate.
* Every decision is stored in the task state and the audit log with who decided.

## Alternatives

* Approvals through the model ("ask the user" tool) - rejected: the model could skip or fake them.
* Fully autonomous with post-hoc review - rejected for production; available for local/dev via policy.

## Consequences

* Non-interactive runs are safe by default (deny) and resumable.
* Explicit confirmations require a human to type the tool name, preventing accidental `y`.
* Tests can script approval sequences deterministically.
