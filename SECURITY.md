# Security Policy

## Threat model

The harness gives an AI agent access to infrastructure tooling. The threats it defends against:

1. **The model asks for something dangerous** (deleting resources, force-pushing, escalating IAM).
   Defence: policy engine + command classifier evaluated outside the model; DESTROY-class actions
   need explicit typed confirmation; forbidden commands are refused regardless of approval.
2. **The model or a tool output tries to redefine policy** (prompt injection in a ticket, log or file).
   Defence: policy is loaded from YAML on disk and cannot be changed at runtime; text from requests,
   tickets and tool results can only make the resolved environment *stricter*.
3. **Credential leakage** into prompts, logs, memory, Jira, PRs or task state.
   Defence: redaction on every audit record, artifact, comment body and memory write; child processes
   get a sanitised environment; tools refuse to write secret-looking content; `printenv`,
   `kubectl get secret -o yaml`, reading `~/.aws/credentials` are FORBIDDEN commands.
4. **Acting on the wrong environment.** Defence: environment identity comes from trusted
   bindings (kube context, AWS account, namespace, host). Unknown = production.
5. **Runaway loops.** Defence: per-task tool-call budget, repeated-call guard, iteration limit.

## Reporting a vulnerability

Open a private security advisory or email the maintainers. Do not file public issues for
exploitable problems. Include reproduction steps, the affected component (policy engine,
classifier, redaction, a backend) and the impact.

## Handling secrets when running the harness

* Provide tokens only via environment variables or a credential provider (`GITHUB_TOKEN`,
  `JIRA_API_TOKEN`, AWS profiles, kubeconfig). Never in `.agent/config.yaml`.
* Run with `--mode read-only` for investigation-only sessions.
* Review `.agent/audit/audit.jsonl` and `tasks/<ID>/` after autonomous runs.
* Use `.agent/policy.yaml` to disable tools you never want the agent to have (`forbidden_tools`).

## Supported versions

The `main` branch receives security fixes. Pin a release tag for production use.
