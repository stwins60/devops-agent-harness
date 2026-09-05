"""Task understanding: classify intent, extract targets and domain hints from a request.

This is deterministic parsing. A model provider (when configured) can refine
the result, but the harness never *depends* on one to route work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.context.environment import infer_hints
from agent.models import TaskKind

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "kubernetes": ["kubernetes", "k8s", "kubectl", "pod", "pods", "deployment", "deployments", "crashloop", "crashloopbackoff", "namespace", "helm",
                   "ingress", "replicaset", "statefulset", "daemonset", "node", "nodes", "worker", "eks", "aks", "gke", "kind", "readiness", "liveness",
                   "oomkilled", "imagepullbackoff", "pending", "hpa", "kustomize", "cluster"],
    "docker": ["docker", "container", "containers", "dockerfile", "compose", "image", "images", "registry"],
    "linux": ["disk", "memory", "cpu", "systemd", "systemctl", "journalctl", "ssh", "host", "server", "vm", "filesystem", "mount", "cron", "kernel",
              "process", "unit", "linux", "ubuntu", "permissions", "certificate"],
    "aws": ["aws", "ec2", "ecs", "eks", "s3", "iam", "rds", "lambda", "cloudwatch", "alb", "nlb", "route53", "vpc", "ecr", "cloudformation", "ssm",
            "secrets manager", "nodegroup", "node group"],
    "terraform": ["terraform", "tfstate", "tf plan", "terraform plan", "terraform apply", "module", "provider", "hcl"],
    "ansible": ["ansible", "playbook", "inventory", "role", "vault"],
    "cicd": ["pipeline", "ci", "cd", "ci/cd", "workflow", "github actions", "gitlab ci", "jenkins", "argocd", "argo", "flux", "build", "job", "runner",
             "action", "actions"],
    "git": ["git", "branch", "merge", "rebase", "pull request", "pr", "merge request", "mr", "commit", "push", "review"],
    "jira": ["jira", "ticket", "issue", "story", "epic", "sprint"],
    "networking": ["dns", "latency", "timeout", "connection refused", "503", "502", "504", "tls", "ssl", "certificate", "port", "firewall", "load balancer",
                   "unreachable", "network", "networking", "routing", "proxy"],
    "observability": ["prometheus", "grafana", "loki", "metrics", "logs", "alert", "alerts", "latency", "error rate", "dashboard", "traces", "datadog", "opentelemetry"],
    "security": ["cve", "vulnerability", "vulnerabilities", "scan", "secret", "secrets", "trivy", "semgrep", "gitleaks", "checkov", "tfsec", "rbac", "security"],
    "incident": ["incident", "outage", "down", "503", "500", "p1", "sev1", "sev2", "on-call", "oncall", "degraded", "returning", "errors", "high"],
}

_STOPWORDS = {"is", "are", "was", "were", "failing", "fails", "failed", "crashing", "crashes", "crashed", "not", "keeps", "keep", "in", "on", "for", "the",
              "a", "an", "my", "our", "this", "that", "with", "and", "or", "to", "of", "status", "logs", "pending", "down", "broken", "restarting", "stuck",
              "running", "error", "errors", "returning", "why", "what", "how", "when", "namespace", "pod", "pods", "deployment", "service", "again", "still"}

_INTENT_PATTERNS = [
    ("plan", r"\b(plan|upgrade|migrate|migration|roll ?out|design|proposal|how (should|would) we)\b"),
    ("fix", r"\b(fix|resolve|repair|remediate|patch|implement|apply|update)\b"),
    ("review", r"\b(review|approve)\b.*\b(pr|pull request|mr|merge request)\b"),
    ("incident", r"\b(incident|outage|is down|returning 5\d\d|high latency|degraded)\b"),
    ("diagnose", r"\b(why|diagnose|debug|investigate|troubleshoot|failing|crash|broken|not working|error|failed)\b"),
]


@dataclass
class Understanding:
    request: str
    kind: TaskKind
    intent: str
    targets: dict[str, Any] = field(default_factory=dict)
    domains: list[str] = field(default_factory=list)
    env_hints: list[str] = field(default_factory=list)
    ticket: Optional[str] = None

    def summary(self) -> str:
        t = ", ".join(f"{k}={v}" for k, v in self.targets.items() if k != "domains")
        return f"kind={self.kind.value} intent={self.intent} domains={','.join(self.domains) or '-'} targets=[{t}]"

    def to_dict(self) -> dict[str, Any]:
        return {"request": self.request, "kind": self.kind.value, "intent": self.intent, "targets": self.targets, "domains": self.domains,
                "env_hints": self.env_hints, "ticket": self.ticket}


def extract_targets(text: str) -> dict[str, Any]:
    t: dict[str, Any] = {}
    low = text.lower()
    m = re.search(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b", text)
    if m:
        t["ticket"] = m.group(1)
    m = re.search(r"\b(deployment|deploy|statefulset|daemonset|svc|service)[/ :]+([a-z0-9][a-z0-9\-]*)", low)
    if m and m.group(2) not in _STOPWORDS:
        t["deployment"] = m.group(2)
    m = re.search(r"\bpod[/ :]+([a-z0-9][a-z0-9\-]*)", low)
    if m and m.group(1) not in _STOPWORDS:
        t["pod"] = m.group(1)
    m = re.search(r"(?:-n\s+|--namespace[= ]|namespace[/ :]+|\bin\s+(?:the\s+)?)([a-z0-9][a-z0-9\-]*)\s+namespace", low) or \
        re.search(r"(?:-n\s+|--namespace[= ]|namespace[/ :]+)([a-z0-9][a-z0-9\-]*)", low)
    if m:
        t["namespace"] = m.group(1)
    m = re.search(r"\b(?:pr|pull request|mr|merge request)\s*#?(\d+)", low)
    if m:
        t["pr"] = int(m.group(1))
    m = re.search(r"\b(?:run|pipeline|workflow|job)\s*#?(\d{2,})", low)
    if m:
        t["run_id"] = int(m.group(1))
    m = re.search(r"https?://[^\s'\"]+", text)
    if m:
        t["url"] = m.group(0)
    m = re.search(r"\b(?:host|server|on)\s+([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+|[a-z0-9\-]*host[a-z0-9\-]*)\b", low)
    if m:
        t["host"] = m.group(1)
    m = re.search(r"\b(?:container|image)\s+([a-z0-9][a-z0-9\-_./:]*)", low)
    if m and m.group(1) not in ("is", "in", "the", "status", "logs", "image", "port", "resource", "resources"):
        t["container"] = m.group(1)
    m = re.search(r"\b(?:the\s+)?([a-z][a-z0-9\-]*)\s+(?:api|service|app)\b", low)
    if m and m.group(1) not in ("the", "my", "our", "production", "staging", "dev", "a", "an", "why", "is", "kubernetes", "k8s", "failing", "this", "fix"):
        t.setdefault("service", m.group(1))
    if re.search(r"\b(api)\b", low) and "service" not in t and "deployment" not in t:
        t["service"] = "api"
    if "deployment" in t and "service" not in t:
        t["service"] = t["deployment"]
    m = re.search(r"\b(?:dir|directory|path|module)\s+([\w./\-]+)", low)
    if m:
        t["dir"] = m.group(1)
    m = re.search(r"\b(?:unit|service)\s+([a-z0-9][a-z0-9\-_.]*\.service)\b", low)
    if m:
        t["unit"] = m.group(1)
    return t


def detect_domains(text: str) -> list[str]:
    low = f" {text.lower()} "
    scores: dict[str, int] = {}
    for domain, words in DOMAIN_KEYWORDS.items():
        s = 0
        for w in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low):
                s += 2 if " " in w else 1
        if s:
            scores[domain] = s
    return [d for d, _ in sorted(scores.items(), key=lambda x: -x[1])]


def understand(request: str, kind_hint: Optional[TaskKind] = None) -> Understanding:
    low = request.lower()
    intent = "question"
    for name, pat in _INTENT_PATTERNS:
        if re.search(pat, low):
            intent = name
            break
    targets = extract_targets(request)
    domains = detect_domains(request)
    kind = kind_hint or TaskKind.QUESTION
    if kind_hint is None:
        if targets.get("ticket") and intent in ("fix", "question") and re.match(r"^\s*(fix|resolve|implement|work)?\s*[A-Z]+-\d+\s*$", request.strip(), re.I):
            kind = TaskKind.JIRA
        elif intent == "incident":
            kind = TaskKind.INCIDENT
        elif intent == "plan":
            kind = TaskKind.PLAN
        elif intent == "fix":
            kind = TaskKind.FIX
        elif intent == "diagnose":
            kind = TaskKind.DIAGNOSE
    if kind == TaskKind.INCIDENT and "incident" not in domains:
        domains.insert(0, "incident")
    if kind == TaskKind.JIRA and "jira" not in domains:
        domains.insert(0, "jira")
    targets["domains"] = domains
    return Understanding(request=request, kind=kind, intent=intent, targets=targets, domains=domains, env_hints=infer_hints(request), ticket=targets.get("ticket"))
