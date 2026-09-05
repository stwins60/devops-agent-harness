# Development

## Local setup

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
make test                                            # 175 tests, no credentials required
make demo                                            # the four definition-of-done commands in --mock
```

## Docker-based environment

```bash
make up            # harness + mock Jira/GitHub API (docker compose)
docker compose run --rm agent devops-agent --mock --yes jira DEVOPS-382
docker compose --profile aws up -d localstack        # optional LocalStack for AWS read APIs
make down
```

The `agent` service is configured so the *real* REST backends point at the mock API server
(`JIRA_URL`, `GITHUB_API_URL`), which lets you test the HTTP clients without `--mock`.

Local Kubernetes: create a kind/k3d cluster, bind its context in `.agent/config.yaml`
(`environments.dev.kube_contexts: [kind-dev]`) and run without `--mock` in `--mode read-only`.

## Mock mode

`tools/mock/world.py` holds the shared state for all mock backends. Scenarios:
`probe-port-mismatch` (default), `oom`, `image-pull`, `pending`, `config-error`, `healthy`,
`ci-failure`, `disk-full`. Failure flags: `jira_unavailable`, `k8s_unreachable`,
`aws_creds_expired`, `git_push_rejected`, `pr_create_fails`, `terraform_plan_fails`,
`tool_timeout`, `rollback_fails`, `partial_deploy`, `permission_denied`, `docker_unavailable`.

```bash
devops-agent --mock --scenario pending "why are my api pods pending?"
devops-agent --mock --flag git_push_rejected --yes jira DEVOPS-382
```

## Project layout conventions

* One package per integration under `tools/` with `build_tools()`, a backend `Protocol`, a real
  backend and a mock backend.
* Specialists never import backends; they call `self.call(inv, tool, args)`.
* Everything user-visible passes through `agent/audit/redaction.py`.

## Tests

`tests/` covers: redaction, command classifier, policy merge/evaluation, approvals, tool
registry/manifest, filesystem sandbox, executor (loop guards, dry-run, rollback, audit),
task state + resume, AGENTS.md hierarchy, environment resolution, runbooks, memory,
Kubernetes scenarios, other specialists, end-to-end workflows, failure recovery, providers +
model decider, MCP client/server, CLI, REST backends against the mock server.

## Release checklist

1. `make test` and `make lint` green.
2. Docs/ADRs updated.
3. `pip install -e .` produces a working `devops-agent` entry point.
