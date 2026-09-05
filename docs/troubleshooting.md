# Troubleshooting the harness

| Symptom | Cause | Fix |
|---|---|---|
| `BLOCKED: cannot reach the cluster` | kubectl context unreachable / unauthorised | check `kubectl config current-context`, VPN, kubeconfig; bind the context in `.agent/config.yaml` |
| `Jira unavailable` | JIRA_URL / credentials missing or network | set `JIRA_URL`, `JIRA_EMAIL`+`JIRA_API_TOKEN` (cloud) or `JIRA_PAT` |
| `no GitHub repository specified` | neither `--repo`-derived config nor `github_repo` | set `github_repo: owner/repo` (or `gitlab_project`) in `.agent/config.yaml` |
| task ends `PAUSED ... resume with` | no approver available (non-interactive) or explicit confirmation required | rerun interactively, or `devops-agent execute TASK-ID --yes` / `--approve-all` |
| `blocked by policy: read-only mode` | mutation requested in read-only/plan mode | use `fix`/`jira`/`incident` (approval mode) or `--mode approval` |
| `direct push/merge to protected branch` | target branch is main/master/production/release/* | the agent always uses feature branches; check `git_push` arguments |
| `Environment resolved as 'unknown'` | no trusted binding matched | add `environments:` bindings; unknown is treated as production |
| `loop guard` in errors | repeated identical tool calls or budget exhausted | raise `limits` cautiously or fix the specialist/model strategy |
| `required binary 'kubectl' is not installed` | real backend without the CLI | install it or run with `--mock`; MCP servers can provide the capability |
| validation FAILED and changes rolled back | tests/scans failed after the change | read `tasks/<ID>/validation.md`; the workspace is restored |
| `ROLLBACK FAILED` in the report | a rollback handler failed | manual intervention; the audit log lists the tool and error |
| PermissionError on `task.json` on Windows | sync client (OneDrive) locked the file | the store retries; move `tasks_dir` outside synced folders for heavy use |

## Where to look

* `tasks/<ID>/task.json` - full state (stage, status, evidence, plan, approvals, tool calls, links, errors)
* `tasks/<ID>/evidence.md`, `plan.md`, `changes.md`, `validation.md`, `final-report.md`, `incident-report.md`
* `.agent/audit/audit.jsonl` - every tool call/approval/stage/rollback/model usage
* `devops-agent tasks show <ID>` - dump the state as JSON
* `--audit-echo` - print audit records live; `--json` - print the final state

## Debugging a specialist

Run in mock mode with the relevant scenario and inspect the evidence:

```bash
devops-agent --mock --scenario oom -q "why is my api deployment failing?" 
devops-agent --mock --flag k8s_unreachable "why is my api deployment failing?"
```

Add analyzers as pure functions over `EvidenceLog` and unit-test them with a hand-built log.
