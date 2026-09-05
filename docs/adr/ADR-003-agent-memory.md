# ADR-003: Agent memory

## Context

The agent should remember architecture decisions, conventions, known infrastructure, common
failure modes, runbooks and previous incidents across tasks, without leaking secrets and without a
database dependency.

## Decision

Persistent memory is a set of markdown files with YAML front matter under `.agent/` in the
project (`memory/`, `decisions/`, `runbooks/`, `architecture/`, `incidents/`, `conventions/`).
`MemoryStore` provides `remember`, `recall` (keyword scoring over title/tags/body) and a
`context_summary` that is injected into the context bundle within a character budget. Writes are
refused when the content matches a secret pattern. Task-level durable state is separate
(`tasks/<ID>/`) and holds evidence, plan, changes, validation, approvals and reports. AGENTS.md
files provide human-authored instructions with hierarchical precedence.

## Alternatives

* Vector database - rejected for Phase 1: extra infrastructure; keyword recall is sufficient for
  project-scale memory and stays reviewable in git.
* Storing memory inside the model context only - rejected: not durable, not auditable.

## Consequences

* Memory is reviewable and diffable; teams can curate it like documentation.
* Recall quality is keyword-based; a vector index can be added behind the same `MemoryStore` API.
* The documentation agent writes failure-mode summaries and incident reports automatically.
