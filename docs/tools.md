# Tools

`devops-agent tools list` prints the registry; `devops-agent tools manifest` prints it as YAML.

## Tool manifest format

```yaml
name: terraform_plan
description: Generate an execution plan and analyse its risk (adds/changes/destroys, sensitive resources).
risk_level: low
requires_approval: false
permission: ANALYZE            # READ | ANALYZE | MODIFY | DEPLOY | DESTROY
permissions: [filesystem.read, terraform.plan]
input_schema: {type: object, properties: {dir: {type: string}, vars: {type: object}}}
output_schema: {}
timeout: 300
rollback: null                 # description or template, e.g. "kubectl rollout undo {kind}/{name} -n {namespace}"
category: terraform
mutating: false
```

## Adapters

| Adapter | Use |
|---|---|
| Native (`FunctionTool`, `Tool`) | Python implementation delegating to a backend Protocol (real + mock) |
| CLI (`CliTool` / `shell_run`) | free-form command; classified SAFE / CAUTION / DANGEROUS / FORBIDDEN by the policy layer |
| MCP (`McpTool`) | tool discovered from an MCP server; risk metadata from config, default MODIFY + approval |
| REST (`RestTool`, `HttpClient`) | HTTP APIs (Jira, GitHub, GitLab, Prometheus, Loki) |
| SDK (`SdkTool`) | wraps an SDK callable (e.g. boto3) |

Structured APIs are preferred over shell scraping: Kubernetes uses `kubectl -o json`, GitHub/GitLab/Jira use REST.

## Catalogue (Phase 1-3)

| Category | Read / analyze | Mutating (approval per policy) |
|---|---|---|
| filesystem | fs_read, fs_list, fs_glob, fs_search | fs_write, fs_replace (rollback: restore previous content) |
| git | git_status, git_diff, git_log, git_current_branch, git_fetch | git_create_branch, git_add, git_commit, git_push (protected branches refused) |
| github | github_get_pr, github_list_prs, github_pr_files, github_pr_comments, github_workflow_runs, github_run_jobs, github_job_logs, github_commits | github_create_pr, github_pr_comment, github_pr_review, github_rerun_workflow |
| gitlab | gitlab_get_mr, gitlab_pipelines, gitlab_pipeline_jobs, gitlab_job_log | gitlab_create_mr, gitlab_mr_note, gitlab_retry_pipeline |
| jira | jira_get_issue, jira_search, jira_get_transitions | jira_add_comment, jira_transition, jira_add_labels, jira_assign, jira_create_subtask, jira_add_worklog, jira_link_issues, jira_update_fields |
| docker | docker_ps, docker_logs, docker_inspect, docker_images, docker_compose_ps | docker_build, docker_restart |
| kubernetes | kubectl_current_context, kubectl_get, kubectl_describe, kubectl_logs, kubectl_events, kubectl_top_pods, kubectl_get_nodes, kubectl_rollout_history, kubectl_rollout_status, kubectl_diff | kubectl_apply (DEPLOY), kubectl_rollout_restart / kubectl_rollout_undo (DEPLOY), kubectl_delete (DESTROY) |
| linux | linux_uptime, linux_disk_usage, linux_memory, linux_top_processes, linux_service_status, linux_failed_units, linux_journal, linux_listening_ports, linux_interfaces, linux_routes, linux_dmesg, linux_os_release, linux_dir_usage, linux_largest_files | linux_service_restart (DEPLOY) |
| networking | net_dns_lookup, net_tcp_check, net_http_check | - |
| aws | aws_identity, aws_describe (read-only operations only), aws_logs_filter | aws_modify (DEPLOY), aws_destroy (DESTROY; delete-*/IAM) |
| terraform | terraform_fmt_check, terraform_validate, terraform_show, terraform_state_list, terraform_plan | terraform_apply (DEPLOY, destroys need allow_destroy), terraform_destroy (DESTROY) |
| ansible | ansible_check, ansible_inventory, ansible_lint | ansible_run (DEPLOY) |
| cicd | cicd_list_runs, cicd_run_jobs, cicd_job_logs (with log analysis) | (reruns via github/gitlab tools) |
| observability | obs_prometheus_query, obs_alerts, obs_loki_query, obs_deployment_timeline, obs_service_health | - |
| security | sec_secret_scan (built-in), sec_k8s_manifest_audit (built-in), sec_trivy_scan, sec_semgrep, sec_gitleaks, sec_checkov | - |
| shell | shell_run (classified) | shell_run (classified) |

## Backends and mocks

Every domain package defines a backend `Protocol`, a real implementation (CLI/REST) and a
`Mock*Backend` reading from `tools/mock/world.py`. `--mock` selects the mocks; scenarios and
failure flags (`--scenario`, `--flag`) shape their behaviour.
