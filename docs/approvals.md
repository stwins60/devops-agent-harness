# Approvals

## When approval is required

The policy engine decides per tool call (`agent/policies/engine.py`):

* the tool declares `requires_approval` (kubectl_apply, git_push, github_create_pr, terraform_apply, ...)
* the permission is DESTROY (always, with explicit confirmation)
* the environment policy lists the tool (`require_approval`, `"*"` for staging/production/unknown)
* the permission exceeds the environment's `auto_allow_max_permission`
* the operating mode is `approval` (every mutation)
* the resolved risk is high/critical

A plan-level gate runs once before implementation; production infrastructure plans need explicit
confirmation. Individual mutating tool calls are gated again during implementation and delivery.

## Prompt

```text
========================================================================
APPROVAL REQUIRED
========================================================================
The following operation will run in PRODUCTION:

  kubectl_apply manifest=<812 chars> namespace=production

Description:
  Patch live deployment production/api probes to port 8080

Resources:
  namespace: production

Risk:
  HIGH

Expected impact:
  running system changes (deploy/restart/apply)

Rollback:
  kubectl rollout undo deployment/api -n production
Approve? [y]es / [n]o / [s]kip / [d]iff / [p]lan / [r]ollback / [?]help:
```

`d` shows the diff/manifest, `p` the plan, `r` the rollback, `s` skips this step, `n` denies.
Explicit confirmations additionally ask you to type `approve <tool>`.

## Handlers

| Handler | Selected by | Behaviour |
|---|---|---|
| interactive | default in a TTY | prompt above |
| auto-deny | `--non-interactive` or no TTY | denies; the task pauses as resumable |
| auto-approve | `--yes` | approves everything except explicit confirmations |
| auto-approve (explicit) | `--approve-all` | approves explicit confirmations too (demos/tests) |
| allowlist | config `mcp_preapproved` / programmatic | approves matching tools or `tool:target` globs, else falls back |

Every decision is stored in `task.json` (`approvals`) and in the audit log.

## Resuming

A paused task keeps its plan: `devops-agent execute TASK-ID --yes` (or interactively) continues
from the approval gate. Denied tasks are final; start a new task to re-plan.

## Dry run

`--dry-run` skips approvals because nothing executes: every mutating tool returns a dry-run result
with the diff it would apply, and the report marks changes as `DRY-RUN`.
