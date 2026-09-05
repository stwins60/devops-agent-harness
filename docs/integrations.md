# IDE and coding-agent integrations

The harness integrates with every client in two ways:

1. **As an MCP server** (`devops-agent mcp-serve`) - your coding agent calls the harness tools; the
   harness enforces policy, approvals, redaction and audit on each call. This is the recommended
   day-to-day setup.
2. **As a model provider** (`devops-agent --provider <name>`) - the harness owns the full lifecycle
   (ticket -> diagnosis -> plan -> approval -> fix -> PR) and uses your agent/model only for reasoning.

## The MCP server

```text
devops-agent [--mock] --project-root <repo> --mode <read-only|approval|autonomous> mcp-serve
```

| Flag | Meaning |
|---|---|
| `--project-root` | repository containing `.agent/config.yaml` (environment bindings, Jira/GitHub settings, limits) and `AGENTS.md` |
| `--mode read-only` | only READ/ANALYZE tools succeed (`kubectl_get`, `terraform_plan`, `jira_get_issue`, ...) |
| `--mode approval` | mutating tools are refused unless pre-approved; the model can plan freely |
| `--mode autonomous` | mutations up to the environment's auto-allow level run without asking; production still needs a human |
| `--mock` | deterministic mock backends, ideal for trying the integration |
| `--env`, `--provider`, `--scenario`, `--flag` | same as the CLI |

Tool names on the client side are usually prefixed with the server name (`devops-agent_kubectl_get`
in OpenCode, `mcp__devops-agent__kubectl_get` in Claude Code). Every response is JSON; failures
carry `error`, `kind` (`auth`, `permission`, `network`, `policy`, `denied`, ...) and `advice`.

Approvals in MCP mode are non-interactive (the server has no terminal). To pre-approve specific
low-risk operations for a session add to `.agent/config.yaml`:

```yaml
mcp_preapproved: ["git_create_branch", "git_add", "git_commit", "jira_add_comment"]
```

Anything else that needs approval is refused with `kind: denied`; run it from the CLI
(`devops-agent execute TASK-ID`) where an interactive prompt is available.

Verify a server outside any IDE:

```bash
printf '%s\n%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | devops-agent --mock mcp-serve
```

Windows: the command is `<venv>\Scripts\devops-agent.exe`; use the full path in IDE configs
(IDEs do not activate virtualenvs). Paths in JSON need `\\` or `/`.

## Claude Code

```bash
claude mcp add devops-agent --scope project -- devops-agent --project-root . --mode approval mcp-serve
claude mcp list          # or /mcp inside Claude Code
```

Project scope writes `.mcp.json` (commit it so the team shares it):

```json
{
  "mcpServers": {
    "devops-agent": {
      "command": "devops-agent",
      "args": ["--project-root", ".", "--mode", "approval", "mcp-serve"],
      "env": {}
    }
  }
}
```

Instructions: Claude Code reads `CLAUDE.md`; copy or symlink `AGENTS.md` to it
(`ln -s AGENTS.md CLAUDE.md`). Suggested first prompts:

* `Use the devops-agent tools to diagnose why deployment api in namespace production is failing. Report FACTs with sources before any conclusion.`
* `Read Jira DEVOPS-382 with devops-agent, inspect the repository and propose a plan. Do not modify anything.`

Allow the tools without per-call prompts by adding `mcp__devops-agent__kubectl_get` etc. to
`permissions.allow` in `.claude/settings.json` (read-only tools only; keep mutations prompted).

Reverse direction: `devops-agent --provider claude-code jira DEVOPS-382` runs `claude -p` with a
structured prompt; the harness parses JSON tool requests and completions.

## OpenCode

Global `~/.config/opencode/opencode.json` or project `opencode.json`:

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
    }
  }
}
```

* `opencode mcp list` must show `devops-agent connected`.
* In the TUI press `tab` to select the `devops` agent; `plan` (built-in) is a good read-only
  companion, or define `devops-readonly` with mutating tools disabled (see `examples/ide/opencode.json`).
* Disable other Jira/GitHub MCPs for the `devops` agent so the model cannot bypass the harness.
* `opencode run "why is my pod crashing?"` for non-interactive use.

Reverse direction: `devops-agent --provider opencode ...` (uses `opencode run --format json`).

## Cursor

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global), then enable under
*Settings -> MCP*. Put `AGENTS.md` content into a rule (`.cursor/rules/devops.mdc`) with
`alwaysApply: true`. Cursor's agent mode asks before running tools by default; keep that on for
mutations.

## VS Code + GitHub Copilot

`.vscode/mcp.json`:

```json
{
  "servers": {
    "devops-agent": {
      "type": "stdio",
      "command": "devops-agent",
      "args": ["--project-root", "${workspaceFolder}", "--mode", "approval", "mcp-serve"]
    }
  }
}
```

Open Copilot Chat -> *Agent* mode -> tools icon -> enable `devops-agent`. Copilot instructions live
in `.github/copilot-instructions.md`; reference `AGENTS.md`. Reverse direction:
`devops-agent --provider copilot ...` (Copilot CLI `copilot -p`, or `gh copilot` fallback).

## Windsurf

`~/.codeium/windsurf/mcp_config.json` with the same `mcpServers` structure as Cursor; enable under
*Cascade -> MCP servers*. Add `AGENTS.md` as a workspace rule (`.windsurfrules`).

## JetBrains (IntelliJ, PyCharm, GoLand, ...)

*Settings -> Tools -> AI Assistant -> Model Context Protocol (MCP) -> +*: command `devops-agent`,
arguments `--project-root <repo> --mode approval mcp-serve`. Junie and the AI chat then list the
tools. Add `AGENTS.md` to the project so Junie picks up the guidelines.

## Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.devops-agent]
command = "devops-agent"
args = ["--project-root", ".", "--mode", "approval", "mcp-serve"]
```

Codex reads `AGENTS.md` natively.

## Gemini CLI

`~/.gemini/settings.json`:

```json
{ "mcpServers": { "devops-agent": { "command": "devops-agent", "args": ["--project-root", ".", "--mode", "approval", "mcp-serve"] } } }
```

Use `GEMINI.md` for instructions (copy `AGENTS.md`).

## Any OpenAI / Anthropic compatible model (no IDE)

```bash
export OPENAI_API_KEY=...            # or OPENAI_BASE_URL=http://localhost:11434/v1 for Ollama
devops-agent --provider openai  jira DEVOPS-382
export ANTHROPIC_API_KEY=...
devops-agent --provider anthropic incident "production API is returning 503"
```

## Recommended team setup

1. Commit `.mcp.json` (Claude Code), `.vscode/mcp.json`, `.cursor/mcp.json` and `opencode.json`
   pointing at `devops-agent --project-root . --mode approval mcp-serve`.
2. Commit `.agent/config.yaml` (bindings, no secrets) and `.agent/policy.yaml` (stricter rules).
3. Commit `AGENTS.md` and make `CLAUDE.md` / `.cursor/rules` / `copilot-instructions.md` point to it.
4. Provide tokens through the developer's shell environment or a secret manager, never in configs.
5. Keep `tasks/` and `.agent/audit/` out of git (already in `.gitignore`) but retained for audit.

## Troubleshooting integrations

| Symptom | Fix |
|---|---|
| server shows "failed" in the IDE | run the command from a terminal; on Windows use the full `.exe` path |
| tools list is empty | `--project-root` points at a directory without read access, or the venv lacks the package: `pip install -e .` |
| every mutation returns `denied` | expected in `--mode approval` over MCP; pre-approve via `mcp_preapproved` or run the step from the CLI |
| `Environment resolved as 'unknown'` | add `environments:` bindings to `.agent/config.yaml` (unknown is treated as production) |
| Jira/GitHub tools fail with `auth` | export `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (or `JIRA_PAT`), `GITHUB_TOKEN` in the environment the IDE inherits |
