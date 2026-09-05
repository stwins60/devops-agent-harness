# Kubernetes agent

## Scope

Pods, Deployments, ReplicaSets, StatefulSets, DaemonSets, Services, Endpoints, Ingress,
ConfigMaps, Secrets (names only, never values), Namespaces, Nodes (versions, taints, allocatable),
resources, probes, scheduling, rollout history. Helm/Kustomize renders are inspected through
`shell_run` (`helm template`, `kustomize build` are SAFE commands).

## Troubleshooting workflow

```text
Deployment -> Pods -> Events -> Container status -> Logs -> Probes -> Resources -> Scheduling -> Networking -> Configuration
```

Each step records FACTs with their tool source. Analyzers then produce hypotheses:

| Analyzer | Confirmed when |
|---|---|
| probe port mismatch | a probe targets a port that is not a containerPort **and** Unhealthy/probe-failed events exist |
| OOMKilled | lastState.terminated.reason == OOMKilled (exit 137 alone is only a hypothesis; Killing events reject it) |
| image pull | ImagePullBackOff/ErrImagePull with the registry error in events |
| unschedulable | FailedScheduling event (insufficient cpu/memory, taints) |
| config error | CreateContainerConfigError + missing ConfigMap key |
| app crash | CrashLoopBackOff with a non-137 exit code and error lines in logs |
| no endpoints | (inference) service has no ready endpoints -> 503 at the edge |
| healthy | all replicas ready |

Example output:

```text
FACT: Pod api-7c98d9b55c-abc12: phase Running, ready=False, restarts=12, waiting reason CrashLoopBackOff, last exit code 137 (Error).
FACT: Event Unhealthy (41x): Readiness probe failed: dial tcp 10.0.1.21:8000: connect: connection refused
FACT: Container api: containerPorts=[8080], probes=readinessProbe->8000/healthz, livenessProbe->8000/healthz.
HYPOTHESIS (confirmed, 95%): Probe port mismatch: readinessProbe checks port 8000 but the container listens on 8080.
```

## Fixes

* Repository available: `fs_replace` on the manifest lines `port: <wrong>` inside workload
  manifests, validated by the manifest consistency check and the project's tests, delivered as a PR.
* No repository: a patched live manifest applied with `kubectl_apply` (DEPLOY, approval; rollback
  `kubectl rollout undo`).
* OOM / image / scheduling / config faults produce guided changes with rollback notes.

## Real cluster usage

`KubectlBackend` uses `kubectl -o json` with the configured `kube_context`. Bind contexts to
environments in `.agent/config.yaml` so production is recognised. Mutations: `kubectl_apply`
(server dry-run in `--dry-run`), `kubectl_rollout_restart/undo`, `kubectl_delete` (DESTROY).
