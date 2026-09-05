# Contributing

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
make test
```

Everything runs without credentials: use `--mock` and the fixtures in `tests/conftest.py`.

## Adding capabilities without touching the orchestrator

| To add | Implement | Register |
|---|---|---|
| a tool | `Tool` subclass or `@tool` function in `tools/<domain>/tools.py` with a `ToolSpec` (permission, risk, rollback, schema) plus a mock backend path | return it from the package's `build_tools()` (picked up by `tools/catalog.py`) |
| a specialist | `Specialist` subclass in `agent/specialists/` with `investigate`, `analyzers`, optional `propose/implement/validate` | `Harness.register_specialist()` or the list in `agent/harness.py` |
| a policy | YAML in `.agent/policy.yaml` (project) - only stricter settings are honoured | automatic |
| a runbook | YAML in `runbooks/<domain>/` or `.agent/runbooks/` following the schema in `docs/runbooks.md` | automatic |
| a provider adapter | class in `adapters/<name>/provider.py` implementing `available()` and `complete()` | `agent/providers/factory.py` |
| an MCP server | entry in `.agent/config.yaml` `mcp_servers` with per-tool risk metadata | automatic |

## Rules for tool authors

1. Declare the honest permission level: anything that changes state is `MODIFY` or higher and `mutating=True`.
2. Provide a rollback (a `rollback()` method or a `rollback` description). If rollback is impossible, say so in the description.
3. Never print or return secret values; use `agent.audit.redaction`.
4. Raise `ToolError(kind=...)` with a classified kind (`auth`, `permission`, `network`, `rate_limit`, `timeout`, `not_found`, `invalid`, `unavailable`).
5. Honour `ctx.dry_run` in every mutating tool.
6. Ship a mock backend and tests for at least one failure path.

## Rules for specialists

* Collect evidence via `self.call(...)` only. Never call backends directly.
* Facts come from tool output; hypotheses need a validation step; conclusions require confirmed hypotheses.
* Prefer runbooks (`self.use_runbook`) before improvising.

## Tests

* Unit, policy, permission, workflow, failure-recovery, MCP and CLI tests live in `tests/`.
* Do not test only the happy path. Every new integration needs at least one "unavailable / denied / rejected" scenario.

## Pull requests

* Branch from `main` as `feature/<TICKET>-<slug>` or `fix/<TICKET>-<slug>`.
* Keep `docs/` and ADRs in sync with behaviour changes. Add an ADR for architectural decisions.
* CI must be green; the security scan must not report blocking findings.
