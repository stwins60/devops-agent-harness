# ADR-001: Model provider abstraction

## Context

The harness must work with Claude Code, OpenCode, GitHub Copilot, hosted APIs (OpenAI,
Anthropic) and local models, and must not break when a vendor changes its SDK. Several of these
"models" are actually coding-agent CLIs without a chat API or native tool calling.

## Decision

Define a minimal provider protocol (`agent/providers/base.py`): `available()` and
`complete(ModelRequest) -> ModelResponse` where the request carries a system prompt, messages and
provider-neutral tool definitions, and the response carries text plus optional structured tool
calls. Adapters live in `adapters/` and use only the standard library (urllib / subprocess).
CLI-driven agents receive a flattened prompt and answer with JSON, which the decider parses.
A `MockProvider` and a `NullProvider` make the harness fully functional and testable without a
model: rule-based specialists produce diagnoses on their own; the model is consulted only when
they cannot conclude.

## Alternatives

* Depend on one vendor SDK - rejected: couples the core to a provider and its release cadence.
* LangChain-style framework - rejected: heavy dependency, hides the control flow the safety model needs.
* Model-first agent (the model decides everything) - rejected: evidence discipline and safety must not depend on model quality.

## Consequences

* Adding a provider is one file plus a factory entry.
* Native tool-calling is used when available (OpenAI, Anthropic); JSON-in-text otherwise.
* Token/latency metrics are recorded per provider in the audit log.
