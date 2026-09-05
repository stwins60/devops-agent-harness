# Security model

## Permission levels

| Level | Meaning | Examples |
|---|---|---|
| READ | observe only | kubectl get/describe/logs, git status, aws describe-* |
| ANALYZE | compute without side effects | terraform plan/validate, kubectl diff, ansible --check, scanners, tests |
| MODIFY | change files, repositories, tickets, non-running config | fs_write, git commit/push, jira comment, kubectl apply to config |
| DEPLOY | change running systems | kubectl rollout, terraform apply, helm upgrade, service restart, aws modify-* |
| DESTROY | irreversible removal or privilege changes | kubectl delete, terraform destroy, aws delete-*, IAM changes, DB migrations |

## Command classification

Free-form shell commands (`shell_run`) are classified by regex rules (`agent/policies/classifier.py`):

* **SAFE** - ls, pwd, git status/diff/log, docker ps/logs, kubectl get/describe/logs, terraform validate/plan, aws describe/list/get, systemctl status, journalctl, ss, dig...
* **CAUTION** - git push, docker build, terraform apply, kubectl apply, helm upgrade, systemctl restart, package installs, chmod/chown, firewall changes...
* **DANGEROUS** - rm -rf, terraform destroy/state rm, kubectl delete/drain, aws delete/terminate, IAM mutations, DROP/TRUNCATE/migrations, force push, iptables -F, shutdown...
* **FORBIDDEN** - rm -rf /, curl | sh, dumping credentials (`~/.aws/credentials`, `printenv`, `kubectl get secret -o yaml`), fork bombs, `AdministratorAccess` attachment.

Pipelines are evaluated as a whole for strict rules first, then per segment (the worst segment wins).
Project rules can add stricter patterns but can never relax built-in DANGEROUS/FORBIDDEN rules.

## Policy enforcement

`policies/default.yaml` defines, per environment, the highest permission the agent may use
without asking (`auto_allow_max_permission`), the tools that always need approval and whether
approvals must be explicit (typed confirmation). A project may add `.agent/policy.yaml`; the
merge keeps only stricter values. Forbidden behaviours: secret output, credential logging,
privilege escalation. Protected branches: main, master, production, release/*.

Workspace tools (filesystem, git, github, gitlab, jira) act on repositories and trackers, not on a
running environment, so the local policy applies to them; their own `requires_approval` still holds
(e.g. `git_push`, `github_create_pr`, `jira_update_fields`).

## Environment identity

Resolved by `agent/context/environment.py` from trusted bindings in `.agent/config.yaml`
(kube contexts, AWS accounts, namespaces, hosts, branches). Declared environments (`--env`,
config) are used when no binding matches; text hints from requests/tickets can only escalate.
Unresolvable = `unknown`, treated exactly like production.

## Secrets

* Redaction patterns: AWS keys, Bearer/Basic tokens, GitHub/GitLab/Slack/OpenAI tokens,
  `password=`/`token=`/`secret=` pairs, JSON secret fields, URL credentials, PEM keys.
* Applied to audit records, task state, artifacts, memory writes, Jira/PR text, tool outputs.
* `fs_write`/`fs_replace` refuse content containing secrets; memory refuses secret content.
* Child processes receive a sanitised environment (secret-looking variables stripped unless a
  backend explicitly passes them through, e.g. AWS credentials for the `aws` CLI).

## Audit log

One JSON object per line in `.agent/audit/audit.jsonl`:

```json
{"timestamp": "...", "event": "tool_call", "agent": "kubernetes-agent", "task": "DEVOPS-382", "tool": "kubectl_get",
 "arguments": {"kind": "pods", "namespace": "production"}, "risk": "low", "permission": "READ", "approval": false,
 "result": "success", "duration": 0.01, "environment": "production", "dry_run": false}
```

## Security agent

Runs the built-in secret scanner and Kubernetes manifest audit (no external binaries) plus
gitleaks, semgrep, checkov and trivy when installed (mocked in `--mock`). Blocking findings fail
validation, which rolls back applied file changes and stops the task.
