# Runbooks

Runbooks are structured YAML procedures the agent consults before improvising. Built-in runbooks
live in `runbooks/<domain>/`; project runbooks in `.agent/runbooks/` or `<project>/runbooks/`.

## Schema

```yaml
name: kubernetes-crashloopbackoff          # unique
description: ...
trigger: [pod crashing, crashloopbackoff]   # phrases matched (word-boundary) against the request
severity: high                              # low | medium | high | critical
tags: [kubernetes, probes]
prechecks:                                  # steps: string or {description, tool, args, command, expected, approval_required}
  - description: Confirm the kubectl context maps to the expected environment
    tool: kubectl_current_context
diagnosis:
  - description: Read Warning events
    tool: kubectl_events
    args: { namespace: $namespace }         # $name placeholders resolve from investigation targets
commands: [kubectl get pods -n <ns>]        # human-readable equivalents
expected_results: [ ... ]
remediation:
  - description: Roll back to the previous revision
    tool: kubectl_rollout_undo
    args: { kind: deployment, name: $deployment, namespace: $namespace }
    approval_required: true
validation: [ ... ]
rollback: [ ... ]
approval_required: true
```

Required fields: name, description, trigger, severity, diagnosis, remediation, validation,
rollback, approval_required. Invalid runbooks are reported by `devops-agent runbooks list` and
skipped.

## How runbooks are used

* Specialists call `use_runbook()` at the start of an investigation; the match is recorded as an
  INFERENCE in the evidence so the report shows which procedure was followed.
* The change planner (`devops-agent plan`) turns prechecks + remediation into plan steps and
  changes, validation into the validation section and rollback into the rollback section.
* Remediation steps with `tool:` become executable changes (still subject to policy/approval).

## Built-in runbooks

kubernetes: crashloopbackoff, pending-pods, oomkilled, worker-node-upgrade;
aws: eks-nodegroup-upgrade; linux: disk-full, service-failed; networking: dns-failure;
database: connection-exhaustion; cicd: pipeline-failure; security: leaked-secret; docker: container-exited.

```bash
devops-agent runbooks list
devops-agent runbooks show kubernetes-crashloopbackoff
devops-agent runbooks find "pods pending insufficient cpu"
```
