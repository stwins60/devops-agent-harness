# Production checklist

## 1. Install

* Python 3.10+ in a dedicated virtualenv or `pipx install "git+https://github.com/stwins60/devops-agent-harness.git"`.
* CLIs the agent should be able to use on that machine: `kubectl`, `docker`, `aws`, `terraform`,
  `ansible-playbook`, scanners (`trivy`, `semgrep`, `gitleaks`, `checkov`). Missing tools degrade
  gracefully (`unavailable` failures, never crashes).
* Run the harness under a user whose credentials are **read-only by default** (a read-only kube
  role, a read-only AWS role). Mutations should require a separate, explicitly granted identity.

## 2. Configure the target repository

```bash
cd /path/to/service-repo
devops-agent init
```

Edit `.agent/config.yaml`:

* `environments:` - bind every kube context, AWS account, namespace and host you use. Anything
  unbound resolves to `unknown`, which is treated as production (all mutations need explicit approval).
* `jira_url`, `github_repo` / `gitlab_project`, `git_provider`, `default_namespace`, `prometheus_url`, `loki_url`.
* `mode: approval` for humans in the loop; `read-only` for shared/CI diagnosis; `autonomous` only
  for local/dev bindings.
* `limits:` - lower `max_tool_calls` for shared servers.
* `mcp_preapproved:` - the few low-risk write tools an IDE session may run without a prompt
  (e.g. `git_create_branch`, `git_add`, `git_commit`, `jira_add_comment`).

Optional `.agent/policy.yaml` (stricter only): `forbidden_tools: [terraform_destroy, aws_destroy]`,
extra `protected_branches`, `tool_overrides`, tighter environment `auto_allow_max_permission`.

Write the `AGENTS.md` (architecture, repositories, deployment, conventions) - it is injected into
every task context and read natively by Claude Code, OpenCode, Codex and Copilot.

## 3. Credentials

Environment variables or a credential provider only:

| Integration | Variables |
|---|---|
| Jira Cloud | `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Jira Server/DC | `JIRA_URL`, `JIRA_PAT` |
| GitHub | `GITHUB_TOKEN` (or `GH_TOKEN`), optional `GITHUB_API_URL` for GHES |
| GitLab | `GITLAB_TOKEN`, `GITLAB_URL` |
| Kubernetes | `KUBECONFIG` (contexts bound in config) |
| AWS | standard chain (`AWS_PROFILE`, SSO, instance role) |
| Models | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`), or the `claude` / `opencode` / `copilot` CLIs |

The harness never writes credentials to config, logs, memory, tickets or PRs; secret-looking
environment variables are stripped from child processes unless a backend explicitly forwards them.

## 4. Verify before granting write access

```bash
devops-agent --mode read-only "why is deployment api failing in production?"   # environment must resolve correctly
devops-agent --mode read-only diagnose kubernetes deployment/api -n production
devops-agent fix DEVOPS-123 --dry-run                                            # plan + diffs, nothing executed
```

Check `.agent/audit/audit.jsonl` and `tasks/<ID>/` after each run.

## 5. Operate

* Interactive terminal: `devops-agent jira KEY` and approve prompts; `devops-agent execute KEY` to resume paused tasks.
* IDE: `devops-agent --project-root . --mode approval mcp-serve` (see `docs/integrations.md`).
* CI: `devops-agent --non-interactive --mode read-only ...` for diagnosis bots; never `--approve-all` in CI.
* Retain `tasks/` and `.agent/audit/` for audit; rotate them with your log tooling.
* Watch the metrics in each final report (tool failures, approval rate, policy blocks, rollback rate).

## 6. Hardening

* Run one harness process per user; do not share an MCP server across identities.
* Keep the harness user out of `docker` group / root unless Docker troubleshooting is required.
* Pin a release tag; review `policies/default.yaml` changes on upgrade.
* Add project runbooks under `.agent/runbooks/` so the agent follows your procedures first.
* Enable the security scanners (`trivy`, `gitleaks`, `semgrep`, `checkov`) on the harness host; findings block delivery.

## 7. Upgrades

`pip install -U "git+https://github.com/stwins60/devops-agent-harness.git@<tag>"`, run `make test`
against `--mock`, then re-run the read-only verification above.
