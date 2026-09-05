# Agent model

## Orchestrator and specialists

The orchestrator never contains domain knowledge. It routes a request to specialists by
keyword/domain score, runs their investigation workflows against a shared `Investigation`
(evidence log + targets), asks a `RootCauseEngine` to turn evidence into a diagnosis, collects
proposals, and drives approval, implementation, validation and delivery.

| Specialist | Investigates | Analyzers (examples) | Proposes / implements |
|---|---|---|---|
| kubernetes-agent | deployment -> pods -> events -> logs -> probes -> resources -> scheduling -> networking -> config | probe port mismatch, OOMKilled, image pull, unschedulable, config error, app crash, no endpoints, healthy | manifest fixes in the repo, live probe patch (approval), memory limits |
| docker-agent | Dockerfile -> containers -> inspect -> logs -> compose | OOM, exit code + log error, port conflict | compose/Dockerfile fixes |
| linux-agent | uptime, disk, memory, failed units, journal, ports, dmesg | disk full, service failed, kernel OOM | reclaim space (DESTROY), restart unit (DEPLOY) |
| jira-agent | ticket, acceptance criteria, comments, links; stages the repo | - | comment / label / transition at the end |
| git-agent | repo layout, status, log; PR review | PR review | branch -> commit -> push -> PR delivery |
| cicd-agent | runs -> failed job -> failed step -> log signatures | log signature | lint auto-fix (F401), guided fixes |
| aws-agent | identity (environment binding), EKS, node groups, EC2, ELB targets, RDS | version skew, unhealthy targets | - (read-only diagnosis) |
| terraform-agent | fmt -> validate -> plan -> risk analysis | invalid config, plan risk | terraform apply (approval; destroys need allow_destroy) |
| ansible-agent | lint -> check mode | check recap | ansible run (approval) + idempotency validation |
| networking-agent | DNS -> TCP -> HTTP | dns/tcp/http failures | - |
| observability-agent | service health snapshot, alerts, deployment timeline, logs | deployment correlation, error rate | - |
| security-agent | secret scan, manifest audit, gitleaks/semgrep/checkov/trivy | findings | blocks validation on critical findings |
| incident-agent | triage + severity, fans out to observability/kubernetes/networking | - | rollback mitigation (approval), incident report |
| documentation-agent | - | - | plan/evidence/changes/validation/final/incident artifacts, memory |

## Operating modes

| Mode | Permitted | Notes |
|---|---|---|
| read-only | READ, ANALYZE | default for questions and `diagnose` |
| plan | READ, ANALYZE | default for `plan`; produces plan.md, never mutates |
| approval | everything, every mutation needs approval | default for `jira`, `fix`, `incident`, `execute` |
| autonomous | mutations auto-allowed up to the environment's `auto_allow_max_permission`; the rest needs approval | production still requires explicit approval for everything |

## Evidence discipline

* `FACT` - observed through a tool, carries its source (`kubectl_get(pod/x)`).
* `HYPOTHESIS` - candidate explanation with a validation step and a status (unvalidated / confirmed / rejected / inconclusive).
* `INFERENCE` - derived statement with a confidence.
* `RECOMMENDATION` - what to do next.

A conclusion is only produced from a confirmed hypothesis. When several are confirmed, hypotheses
from the specialists the request was routed to first take precedence, then confidence.

## Model providers

The rule-based specialists work with no model at all. When a provider is configured and the
specialists cannot conclude, `ModelDecider` runs a bounded OBSERVE -> THINK -> ACT loop: the model
proposes tool calls (JSON or native tool-use), the executor applies policy/approval/audit, and the
model's final structured answer is converted to evidence and hypotheses. A model-only hypothesis
cannot be "confirmed" without tool-derived facts.

Providers: `mock`, `none`, `openai` (OpenAI/Azure/Ollama/vLLM), `anthropic`, `claude-code`
(CLI), `opencode` (CLI), `copilot` (CLI). Select with `--provider` or `provider:` in config.

## Loop safeguards

* `limits.max_tool_calls` per task, `limits.max_repeated_calls` identical calls, `limits.max_iterations` model turns.
* Failures are classified (`auth`, `permission`, `network`, `rate_limit`, `timeout`, `not_found`, `invalid`, `unavailable`);
  only read-only `network`/`rate_limit` failures are retried (bounded); `auth`/`permission` never are.
