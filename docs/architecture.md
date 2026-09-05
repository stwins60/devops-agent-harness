# Architecture

## Layers

```text
apps/cli/main.py          CLI (question | jira | incident | diagnose | plan | fix | execute | resume | tools | runbooks | mcp-serve | init)
agent/harness.py          composition root: config -> policy -> registry/backends -> approvals -> executor -> memory -> runbooks -> provider -> specialists
agent/orchestrator/       lifecycle (orchestrator.py), request understanding (understanding.py), model loop (decider.py)
agent/specialists/        domain agents (kubernetes, docker, linux, jira, git, cicd, aws, terraform, ansible, networking, observability, security, incident, documentation)
agent/planners/           change planner for PLAN-kind requests (runbook + evidence + proposals)
agent/executor.py         single choke point: policy -> approval -> tool -> audit -> state -> rollback bookkeeping
agent/policies/           YAML policy + command classifier (enforced outside the model)
agent/approvals/          approval handlers (interactive, allowlist, auto, recording)
agent/audit/              redaction + JSONL audit logger + metrics
agent/state/              durable task state (tasks/<ID>/task.json + markdown artifacts)
agent/context/            AGENTS.md discovery, environment resolution, context bundle
agent/memory/             project memory under .agent/
agent/runbooks/           runbook loader/matcher
agent/rca/                evidence log + root-cause engine (FACT / HYPOTHESIS / INFERENCE / RECOMMENDATION)
agent/rollback/           rollback plan/engine
agent/reports/            markdown renderers (plan, evidence, changes, validation, final report, incident report)
agent/providers/          provider protocol, mock/null providers, factory
agent/mcp/                MCP client (consume servers) and MCP server (expose harness tools)
tools/                    registry, adapters (CLI/MCP/REST/SDK), shell runner, http client, per-domain tools with real + mock backends
adapters/                 model provider adapters (openai-compatible, anthropic + claude-code, opencode, copilot) and generic MCP registration
```

## Lifecycle

`Orchestrator._lifecycle` implements the stages below. Each stage is skipped on resume when the
persisted `task.stage` is already past it, so an interrupted task continues from where it stopped.

| Stage | What happens | Stops when |
|---|---|---|
| task_understanding | intent, kind, domains, targets, environment hints; specialist routing | - |
| context_discovery | repo (branch/commit/remote), AGENTS.md hierarchy, trusted environment resolution, ticket, memory, runbooks | - |
| inspection | each routed specialist runs its read-only workflow; Jira ticket text can pull in more specialists | integration unavailable / permission denied -> BLOCKED |
| root_cause_analysis | analyzers turn facts into hypotheses; a conclusion needs a confirmed hypothesis; model decider only if rule-based analysis cannot conclude | - |
| plan | specialists propose changes; PLAN kind builds a full change plan from runbooks + evidence | - |
| risk_assessment | risk level from changes + environment; plan.md written | read-only kinds/modes stop here (COMPLETED) |
| approval_gate | plan-level approval (explicit for production infrastructure) | denied -> DENIED, no approver -> PAUSED (resumable) |
| implementation | owner specialist executes the changes through the executor (per-tool policy + approval again) | denial/policy block -> stop |
| validation | owner validation + git diff + project tests + security scans; failures trigger automatic rollback | failure -> FAILED |
| documentation | evidence.md, changes.md, validation.md | - |
| external_update | branch/commit/push/PR via git-agent; Jira comment/labels/transition | push/PR denied -> stop |
| final_report | final-report.md (+ incident-report.md), memory, metrics | COMPLETED |

## Extension points

* **Tool**: implement `tools.base.Tool` (or `@tool`), return it from a package `build_tools()`.
* **Agent**: subclass `Specialist`; register with `Harness.register_specialist`.
* **Policy**: `.agent/policy.yaml` (stricter only).
* **Runbook**: YAML in `runbooks/` or `.agent/runbooks/`.
* **Provider adapter**: implement `ModelProvider` and add it to the factory.
* **MCP**: `mcp_servers` in config to consume; `devops-agent mcp-serve` to expose.

## Multi-repository support

A task's workspace is one repository (`--repo` or the repository named by the ticket). The
Jira specialist stages the repo into `tasks/<ID>/workspace`. Application/GitOps/Terraform
repositories are discovered through `AGENTS.md` (repositories section) and `default_repo_path`;
cross-repository orchestration (application -> image -> GitOps -> cluster) is a Phase 4
extension: add a `RepositoryGraph` to `agent/context` and let specialists request additional
workspaces through the same `Investigation.targets` mechanism.

## Observability of the harness

`.agent/audit/audit.jsonl` contains `tool_call`, `approval`, `stage`, `policy_block`,
`rollback`, `model_usage` and `metrics` events. `Metrics.snapshot()` reports tool calls,
failures, approvals, rollbacks, model calls, token usage, per-tool timings and derived rates;
the final report embeds them.
