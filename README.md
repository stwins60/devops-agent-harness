# DevOps Agent Harness

A production-grade, **model-agnostic** harness that turns an AI coding agent (Claude Code,
OpenCode, GitHub Copilot, Cursor, Windsurf, Codex CLI, Gemini CLI, or any OpenAI/Anthropic
compatible model) into a governed DevOps engineer. Policy, approvals, audit logging, secret
redaction and rollback are enforced **outside the model**, so the agent can troubleshoot
Kubernetes, Docker, Linux, AWS, Terraform, Ansible, CI/CD and Git, work Jira tickets end to end,
run incident response and produce evidence-backed reports without ever being trusted with
unchecked write access.

```text
USER REQUEST -> TASK UNDERSTANDING -> CONTEXT DISCOVERY -> INSPECTION -> ROOT CAUSE ANALYSIS
-> PLAN -> RISK ASSESSMENT -> APPROVAL GATE -> IMPLEMENTATION -> VALIDATION -> DOCUMENTATION
-> JIRA / PR UPDATE -> FINAL REPORT
```

[![CI](https://github.com/stwins60/devops-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/stwins60/devops-agent-harness/actions/workflows/ci.yml)

---

## Table of contents

1. [What it does](#what-it-does)
2. [Quick start (5 minutes, no credentials)](#quick-start)
3. [Installation for production](#installation-for-production)
4. [Configuration](#configuration)
5. [Using it from your IDE / coding agent](#using-it-from-your-ide--coding-agent)
   - [Claude Code](#claude-code) - [OpenCode](#opencode) - [Cursor](#cursor) - [VS Code + Copilot](#vs-code--github-copilot)
   - [Windsurf](#windsurf) - [JetBrains](#jetbrains-ides) - [Codex CLI / Gemini CLI](#codex-cli-and-gemini-cli) - [Any other MCP client](#any-other-mcp-client)
6. [Using it as a model-driven CLI](#using-it-as-a-model-driven-cli)
7. [Safety model](#safety-model)
8. [Architecture](#architecture)
9. [Operations: audit, metrics, task state, resume](#operations)
10. [Documentation](#documentation)

---

## What it does

| Command | Behaviour |
|---|---|
| `devops-agent "why is my pod crashing?"` | read-only investigation; evidence-backed root cause (FACT / HYPOTHESIS / INFERENCE / RECOMMENDATION) |
| `devops-agent jira DEVOPS-382` | read ticket -> locate repo -> diagnose -> plan -> approval -> fix -> tests + security scans -> branch -> commit -> push -> PR -> Jira comment + transition -> report |
| `devops-agent incident "production API is returning 503"` | triage, severity, metrics/logs/deployment correlation, mitigation (rollback) with approval, postmortem |
| `devops-agent plan "upgrade our Kubernetes worker nodes"` | complete change plan from runbooks + live evidence; mutates nothing |
| `devops-agent execute TASK-ID` / `resume TASK-ID` | continue a paused task; only policy-permitted, approved actions run |
| `devops-agent diagnose kubernetes deployment/api -n production` | targeted diagnosis |
| `devops-agent fix DEVOPS-382 --dry-run` | full plan and diffs, nothing executed |
| `devops-agent mcp-serve` | expose all 115 governed tools to any MCP client (Claude Code, OpenCode, Cursor, ...) |

Specialist agents: kubernetes, docker, linux, jira, git/PR, cicd, aws, terraform, ansible,
networking, observability, security, incident response, documentation.

---

## Quick start

```bash
git clone https://github.com/stwins60/devops-agent-harness.git
cd devops-agent-harness
python -m venv .venv && . .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# everything below runs against deterministic mock infrastructure, no credentials needed
devops-agent --mock "Why is my Kubernetes API deployment failing?"
devops-agent --mock --yes jira DEVOPS-382
devops-agent --mock --approve-all incident "production API is returning 503"
devops-agent --mock plan "upgrade our Kubernetes worker nodes"
devops-agent --mock fix DEVOPS-382 --dry-run
make test                                             # 175 tests
```

Mock scenarios: `--scenario probe-port-mismatch|oom|image-pull|pending|config-error|healthy|ci-failure|disk-full`.
Failure injection: `--flag jira_unavailable|k8s_unreachable|aws_creds_expired|git_push_rejected|pr_create_fails|terraform_plan_fails|tool_timeout|rollback_fails|partial_deploy|permission_denied`.

---

## Installation for production

Requirements: Python 3.10+, `git`. Optional CLIs used when present: `kubectl`, `docker`,
`aws`, `terraform`, `ansible-playbook`, `trivy`, `semgrep`, `gitleaks`, `checkov`.

```bash
# 1. install (a dedicated venv or pipx)
pipx install "git+https://github.com/stwins60/devops-agent-harness.git"
# or: pip install "git+https://github.com/stwins60/devops-agent-harness.git"

# 2. initialise the repository the agent should operate on
cd /path/to/your/service-repo
devops-agent init            # creates .agent/config.yaml, .agent/{memory,decisions,runbooks,...}, AGENTS.md skeleton

# 3. provide credentials through the environment ONLY (never in config files)
export JIRA_URL=https://your-company.atlassian.net JIRA_EMAIL=you@company.com JIRA_API_TOKEN=...
export GITHUB_TOKEN=...            # or GITLAB_TOKEN + GITLAB_URL
export KUBECONFIG=~/.kube/config   # kube contexts are bound to environments in .agent/config.yaml
export AWS_PROFILE=readonly        # the standard AWS credential chain

# 4. first run in read-only mode to confirm environment resolution
devops-agent --mode read-only "why is deployment api failing in production?"
```

Docker: `docker compose up -d` starts the harness with a mock Jira/GitHub API server; see
`docs/development.md`. A production checklist is in `docs/production.md`.

---

## Configuration

`.agent/config.yaml` in the target repository (see `examples/config.example.yaml`):

```yaml
mode: approval                 # read-only | plan | approval | autonomous
environment: dev               # declared; trusted bindings below can only make it stricter
provider: auto                 # auto | mock | none | openai | anthropic | claude-code | opencode | copilot | ollama
jira_url: https://your-company.atlassian.net
github_repo: your-org/service-repo
git_provider: github           # or gitlab (+ gitlab_project)
default_namespace: production
prometheus_url: http://prometheus.monitoring:9090
environments:                  # trusted identity -> environment; unknown == production
  production: { kube_contexts: [prod-eks], aws_accounts: ["123456789012"], namespaces: [production] }
  staging:    { kube_contexts: [staging-eks], namespaces: [staging] }
  dev:        { kube_contexts: [kind-dev, docker-desktop], namespaces: [dev, default] }
mcp_servers: []                # optional: consume other MCP servers as governed tools
```

Optional `.agent/policy.yaml` makes the built-in policy **stricter** (never looser): extra
protected branches, forbidden tools, per-tool approval overrides, tighter environment limits.
See `examples/policy.example.yaml` and `docs/security.md`.

Environment variables override config: `DEVOPS_AGENT_MODE`, `DEVOPS_AGENT_ENV`,
`DEVOPS_AGENT_PROVIDER`, `DEVOPS_AGENT_NAMESPACE`, `DEVOPS_AGENT_GITHUB_REPO`, `DEVOPS_AGENT_TASKS_DIR`,
`DEVOPS_AGENT_NON_INTERACTIVE`, `DEVOPS_AGENT_MOCK`.

---

## Using it from your IDE / coding agent

There are two integration directions. You can use both.

| Direction | How | When |
|---|---|---|
| **Your agent uses the harness** (recommended) | run `devops-agent mcp-serve` as an MCP server; the agent gets 115 governed tools (`kubectl_*`, `jira_*`, `git_*`, `aws_*`, `terraform_*`, ...) and every call is policy-checked, audited and redacted | day-to-day work inside Claude Code / OpenCode / Cursor / VS Code / Windsurf |
| **The harness uses your agent as its model** | `devops-agent --provider claude-code|opencode|copilot|openai|anthropic|ollama ...` | scripted / CI runs of the full lifecycle (Jira ticket -> PR) where the harness owns the workflow |

The MCP server command is the same everywhere:

```text
devops-agent --project-root /path/to/repo --mode approval mcp-serve
```

* `--project-root` is the repository containing `.agent/config.yaml` (environment bindings, Jira/GitHub settings).
* `--mode read-only` for investigation-only sessions; `--mode approval` (default) lets the model
  investigate freely while every mutation still needs approval; `--mode autonomous` auto-allows
  low-risk mutations per environment policy (production always needs a human).
* Add `--mock` to try any IDE integration without infrastructure.

In MCP mode approvals are **non-interactive by default**, so a mutation that needs approval is
refused with an explanatory error and the task is left resumable. Approve it from a terminal with
`devops-agent execute TASK-ID` (interactive prompt) or run the harness CLI directly for that step.
Copy-paste-ready config files for every client are in `examples/ide/`.

### Claude Code

```bash
# project scope (writes .mcp.json, shareable with the team)
claude mcp add devops-agent --scope project -- devops-agent --project-root . --mode approval mcp-serve
# or user scope
claude mcp add devops-agent -- devops-agent --project-root /path/to/repo --mode approval mcp-serve
```

`.mcp.json` equivalent (`examples/ide/claude-code.mcp.json`):

```json
{ "mcpServers": { "devops-agent": { "command": "devops-agent",
    "args": ["--project-root", ".", "--mode", "approval", "mcp-serve"] } } }
```

Claude Code reads `AGENTS.md`/`CLAUDE.md`; symlink or copy the generated `AGENTS.md` to
`CLAUDE.md` so the conventions and approval rules are in its context. In Claude Code, run
`/mcp` to confirm `devops-agent` is connected, then ask in plain language:
`Use devops-agent to find out why deployment api in production is failing.`

To let the harness drive Claude Code instead: `devops-agent --provider claude-code jira DEVOPS-382`
(requires the `claude` CLI on PATH).

### OpenCode

Add to `~/.config/opencode/opencode.json` (global) or `opencode.json` in the project
(`examples/ide/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "devops-agent": {
      "type": "local",
      "command": ["devops-agent", "--project-root", "/path/to/repo", "--mode", "approval", "mcp-serve"],
      "enabled": true
    }
  },
  "agent": {
    "devops": {
      "description": "DevOps engineer using the governed devops-agent tools",
      "mode": "primary",
      "prompt": "{file:/path/to/repo/AGENTS.md}",
      "tools": { "devops-agent*": true, "atlassian*": false, "github*": false, "gitlab*": false }
    },
    "devops-readonly": {
      "description": "Read-only investigation, never mutates",
      "mode": "primary",
      "prompt": "{file:/path/to/repo/AGENTS.md}\n\nREAD-ONLY: inspect only, never change anything.",
      "tools": { "devops-agent*": true, "devops-agent_kubectl_apply": false, "devops-agent_kubectl_delete": false,
                 "devops-agent_git_push": false, "devops-agent_github_create_pr": false, "devops-agent_terraform_apply": false,
                 "devops-agent_aws_modify": false, "devops-agent_fs_write": false, "devops-agent_fs_replace": false,
                 "devops-agent_shell_run": false }
    }
  }
}
```

Then `opencode mcp list` should show `devops-agent connected`; press `tab` in the TUI and pick
the `devops` agent. Disabling the raw `atlassian*`/`github*` MCPs for that agent prevents the model
from bypassing the harness's policy by talking to Jira/GitHub directly. Tool names inside OpenCode
are `devops-agent_<tool>`. Windows: use the full path to `.venv\Scripts\devops-agent.exe` in `command`.

Reverse direction: `devops-agent --provider opencode "why is my pod crashing?"` (uses `opencode run`).

### Cursor

`.cursor/mcp.json` in the project or `~/.cursor/mcp.json` (`examples/ide/cursor.mcp.json`):

```json
{ "mcpServers": { "devops-agent": { "command": "devops-agent",
    "args": ["--project-root", ".", "--mode", "approval", "mcp-serve"] } } }
```

Enable it under *Settings -> MCP*. Add `AGENTS.md` to *Rules* (or reference it from `.cursor/rules`)
so the agent follows the conventions.

### VS Code + GitHub Copilot

`.vscode/mcp.json` (`examples/ide/vscode.mcp.json`):

```json
{ "servers": { "devops-agent": { "type": "stdio", "command": "devops-agent",
    "args": ["--project-root", "${workspaceFolder}", "--mode", "approval", "mcp-serve"] } } }
```

Open Copilot Chat in *Agent* mode, click the tools icon and enable `devops-agent`. Copilot reads
`.github/copilot-instructions.md`; point it at `AGENTS.md` or copy the relevant sections.
Reverse direction: `devops-agent --provider copilot ...` (uses the Copilot CLI).

### Windsurf

`~/.codeium/windsurf/mcp_config.json` (`examples/ide/windsurf.mcp.json`) uses the same
`mcpServers` shape as Cursor. Enable the server under *Cascade -> MCP*.

### JetBrains IDEs

*Settings -> Tools -> AI Assistant -> Model Context Protocol -> Add*, command `devops-agent`,
arguments `--project-root <repo> --mode approval mcp-serve`. Junie and the AI Assistant chat then
see the tools.

### Codex CLI and Gemini CLI

Codex (`~/.codex/config.toml`, `examples/ide/codex.config.toml`):

```toml
[mcp_servers.devops-agent]
command = "devops-agent"
args = ["--project-root", ".", "--mode", "approval", "mcp-serve"]
```

Gemini CLI (`~/.gemini/settings.json`, `examples/ide/gemini.settings.json`):

```json
{ "mcpServers": { "devops-agent": { "command": "devops-agent",
    "args": ["--project-root", ".", "--mode", "approval", "mcp-serve"] } } }
```

### Any other MCP client

The server speaks MCP over stdio (`initialize`, `tools/list`, `tools/call`). Any client that can
launch a local command works. Verify manually:

```bash
printf '%s\n%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | devops-agent --mock mcp-serve
```

Full per-client details, troubleshooting and prompt suggestions: `docs/integrations.md`.

---

## Using it as a model-driven CLI

The rule-based specialists produce diagnoses **without any model**. A model is consulted only
when they cannot conclude, and its tool requests still pass through policy and approval.

```bash
devops-agent --provider anthropic  jira DEVOPS-382         # ANTHROPIC_API_KEY
devops-agent --provider openai     incident "..."          # OPENAI_API_KEY (+ OPENAI_BASE_URL for Azure/vLLM)
devops-agent --provider ollama     "why is my pod crashing?"  # local OpenAI-compatible server
devops-agent --provider claude-code plan "..."            # claude CLI
devops-agent --provider opencode   "..."                    # opencode CLI
devops-agent --provider copilot    "..."                    # copilot CLI
```

Interactive approvals show the operation, environment, risk, expected impact, rollback, and
accept `y / n / s(kip) / d(iff) / p(lan) / r(ollback)`. DESTROY-class and production operations
require typing `approve <tool>`. `--yes` auto-approves non-explicit prompts; `--approve-all` is for
demos only. CI usage: `--non-interactive` (denies and pauses; resume from a terminal).

---

## Safety model

* **Permission levels** READ < ANALYZE < MODIFY < DEPLOY < DESTROY on every tool.
* **Command classification** SAFE / CAUTION / DANGEROUS / FORBIDDEN for shell commands
  (`rm -rf /`, `curl | sh`, credential dumps are refused outright; `terraform destroy`,
  `kubectl delete`, IAM changes, DB migrations, force pushes always need explicit approval).
* **Environment identity** from trusted bindings (kube context, AWS account, namespace, host);
  request/ticket text can only make it stricter; unknown == production.
* **Policy** (`policies/default.yaml`) per environment; project policy can only tighten it.
* **Protected branches** (`main`, `master`, `production`, `release/*`) are never pushed to.
* **Secrets** redacted from every log, artifact, comment and memory write; child processes get a sanitised environment.
* **Audit log** `.agent/audit/audit.jsonl` (tool calls, approvals, stages, rollbacks, model usage, metrics).
* **Rollback plan** recorded for every mutation; validation failures roll back automatically; impossible rollbacks are stated.
* **Loop guards**: tool-call budget, repeated-call detection, model iteration limit.

---

## Architecture

```text
                +----------------------+
                | USER / IDE / MCP CLI |
                +----------+-----------+
                           v
                +----------------------+
                |    AGENT HARNESS     |  agent/harness.py
                +----------------------+
       +-------------------+-------------------+
       v                   v                   v
  Orchestrator        Policy Engine      Approval Engine
       v
  Specialist Agents (kubernetes, docker, linux, jira, git, cicd, aws, terraform, ansible,
                     networking, observability, security, incident, documentation)
       v
  Tool Executor  ->  policy -> approval -> tool -> audit + task state + rollback plan
       v
  Tool Registry (115 tools)  ->  Native | CLI | REST | MCP | SDK backends (real + mock)
```

Extension points: add a **Tool** (package `build_tools()`), an **Agent** (`Specialist`
subclass), a **Policy** (`.agent/policy.yaml`), a **Runbook** (YAML), or a **Provider
adapter** without touching the orchestrator. Details in `docs/architecture.md`.

---

## Operations

* Task state: `tasks/<ID>/task.json` plus `plan.md`, `evidence.md`, `changes.md`,
  `validation.md`, `final-report.md`, `incident-report.md`. `devops-agent tasks list|show ID`.
* Resume: `devops-agent resume ID` / `execute ID` continues from the recorded stage.
* Audit and metrics: `.agent/audit/audit.jsonl`; the final report embeds tool calls, failures,
  approvals, policy blocks and model calls.
* Memory: `.agent/{memory,decisions,runbooks,architecture,incidents,conventions}` markdown, secret-checked.
* Runbooks: `devops-agent runbooks list|show|find`.

---

## Documentation

`docs/integrations.md` (IDE / agent setup), `docs/production.md` (go-live checklist),
`docs/architecture.md`, `docs/agent-model.md`, `docs/tools.md`, `docs/security.md`,
`docs/approvals.md`, `docs/jira.md`, `docs/kubernetes.md`, `docs/aws.md`, `docs/terraform.md`,
`docs/runbooks.md`, `docs/troubleshooting.md`, `docs/development.md`, ADRs in `docs/adr/`.
`CONTRIBUTING.md`, `SECURITY.md`. License: Apache-2.0.
