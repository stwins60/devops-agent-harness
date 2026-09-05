# ADR-002: Tool permission system

## Context

Tools range from harmless (`kubectl get`) to catastrophic (`terraform destroy`). Free-form shell
access is needed for real DevOps work, but the model must never be the party deciding what is safe.

## Decision

* Every tool declares a `ToolSpec` with an ordered permission level (READ < ANALYZE < MODIFY <
  DEPLOY < DESTROY), a risk level, `requires_approval`, permissions, timeout and rollback.
* Free-form commands are classified by regex rules into SAFE / CAUTION / DANGEROUS / FORBIDDEN;
  strict rules are matched on the full command line before pipeline splitting; configured rules can
  only add stricter classifications.
* A single executor is the only path to `tool.run()`. It evaluates the YAML policy
  (`policies/default.yaml` merged with a stricter-only project policy) against the resolved
  environment and operating mode, requests approval, executes, audits, records state and rollback.
* Environment identity is resolved from trusted bindings; unknown environments are treated as production.
* Workspace tools (filesystem, git, github, gitlab, jira) use the local environment policy because
  they act on repositories/trackers, not running systems; their own approval flags still apply.

## Alternatives

* Letting the model self-classify risk - rejected: violates "never allow the LLM to redefine policy".
* Allow/deny lists of tool names only - rejected: cannot handle shell pipelines or environment differences.
* OPA/Rego policies - deferred: the YAML policy is simpler for Phase 1; the engine boundary allows swapping in OPA later.

## Consequences

* Every mutation is auditable and attributable to a policy decision and (where required) a human.
* Adding a tool requires an honest permission declaration; tests assert consistency (mutating => MODIFY+, DESTROY => approval).
* Unknown commands default to CAUTION (approval), never SAFE.
