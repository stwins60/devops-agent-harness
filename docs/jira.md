# Jira integration

## Configuration

```bash
export JIRA_URL=https://your-company.atlassian.net
export JIRA_EMAIL=you@example.com          # Jira Cloud: email + API token (basic auth, REST v3)
export JIRA_API_TOKEN=...
# or, for Jira Server/Data Center:
export JIRA_PAT=...                          # bearer token (REST v2)
```

`--mock` uses `MockJiraBackend` with the `DEVOPS-382` fixture. The mock HTTP server
(`python -m apps.mockserver.server`) also serves the Jira REST API for testing the real client.

## What the agent reads

issue key, summary, description (ADF converted to text), acceptance criteria (parsed from the
description), comments, attachments (names), status, priority, labels, components, assignee,
reporter, linked issues, epic/parent, sprint (when exposed), worklogs.

## What the agent writes

`jira_add_comment`, `jira_transition`, `jira_add_labels`, `jira_assign`, `jira_create_subtask`,
`jira_add_worklog`, `jira_link_issues`, `jira_update_fields` - all MODIFY tools; sub-tasks and
field updates require approval. Comment bodies are refused if they contain a secret.

## Ticket workflow (`devops-agent jira DEVOPS-382`)

1. Fetch the ticket (jira-agent) and derive targets: repository, service, namespace, cluster.
2. Stage the repository as the task workspace (`tasks/DEVOPS-382/workspace`, copied from the
   path named by the ticket / `--repo` / `default_repo_path`; never the original checkout).
3. Route domain specialists from the ticket text (Kubernetes, CI, Terraform, ...), inspect, diagnose.
4. Build a plan (files, risks, rollback, validation); plan-level approval gate.
5. Implement file changes via `fs_replace`/`fs_write` (rollback recorded).
6. Validate: manifest consistency, project tests (`pytest`), git diff, security scans.
7. Deliver: `fix/DEVOPS-382-<slug>` branch -> commit (`DEVOPS-382: ...`) -> push (approval) -> PR (approval).
8. Update Jira: comment with root cause / validation / PR link, label `devops-agent`, transition to
   `In Review` (PR opened) or `In Progress` (changes applied but not delivered).
9. Final report with the traceability chain Jira -> branch -> commit -> PR.

Failure handling: Jira unavailable -> BLOCKED before any change; push rejected / PR failure ->
changes stay on the branch, ticket moves to In Progress with an explanatory comment; validation
failure -> automatic rollback of file changes, task FAILED.
