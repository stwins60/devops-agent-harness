"""Environment identity resolution.

The environment (local/dev/qa/staging/production) decides which policy
applies. It must come from trusted sources only:

1. ``--env`` CLI flag *combined with* a matching trusted binding, or the flag
   alone when it is at least as strict as what bindings suggest
2. ``.agent/config.yaml`` ``environment:`` / ``environments:`` bindings that
   map kube contexts, AWS accounts, namespaces or hosts to environments
3. ``DEVOPS_AGENT_ENV`` environment variable

Anything supplied by the model, a Jira ticket or a free-text request is at
most a *hint*: it can make the resolution stricter, never more permissive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent.config import HarnessConfig
from agent.models import Environment


@dataclass
class EnvironmentResolution:
    environment: Environment
    source: str
    evidence: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.source in ("config", "binding", "flag+binding", "env", "flag") and self.environment != Environment.UNKNOWN


def resolve_environment(config: HarnessConfig, *, kube_context: Optional[str] = None, aws_account: Optional[str] = None,
                        namespace: Optional[str] = None, host: Optional[str] = None, branch: Optional[str] = None,
                        untrusted_hints: Optional[list[str]] = None) -> EnvironmentResolution:
    evidence: list[str] = []
    bound = config.environment_for(kube_context=kube_context, aws_account=aws_account, namespace=namespace, host=host, branch=branch)
    if bound is not None:
        evidence.append(f"trusted binding matched (context={kube_context}, account={aws_account}, namespace={namespace}, host={host})")

    declared = config.environment if config.environment != Environment.UNKNOWN else None
    declared_source = config.environment_source

    if bound is not None and declared is not None:
        # when both exist the stricter one wins; disagreement is recorded
        if bound.strictness >= declared.strictness:
            env, source = bound, "binding"
            if bound != declared:
                evidence.append(f"declared environment '{declared.value}' overridden by stricter binding '{bound.value}'")
        else:
            env, source = declared, f"{declared_source}+binding"
            evidence.append(f"declared environment '{declared.value}' is stricter than binding '{bound.value}'")
    elif bound is not None:
        env, source = bound, "binding"
    elif declared is not None:
        env, source = declared, declared_source
        evidence.append(f"environment declared via {declared_source}")
    else:
        env, source = Environment.UNKNOWN, "unverified"
        evidence.append("no trusted environment binding found; treating as production-equivalent (strictest policy)")

    hints = list(untrusted_hints or [])
    for hint in hints:
        h = Environment.parse(hint)
        if h != Environment.UNKNOWN and h.strictness > env.strictness and env != Environment.UNKNOWN:
            evidence.append(f"untrusted hint '{hint}' is stricter than resolved '{env.value}'; escalating to '{h.value}'")
            env = h
            source = f"{source}+hint-escalation"
    return EnvironmentResolution(env, source, evidence, hints)


def infer_hints(text: str) -> list[str]:
    """Extract environment words from free text. These are hints only."""
    low = (text or "").lower()
    hints = []
    for word in ("production", "prod", "staging", "stage", "qa", "uat", "dev", "development", "local"):
        if f" {word} " in f" {low} " or f"{word}-" in low or f"-{word}" in low or f"{word}/" in low or f"/{word}" in low:
            hints.append(word)
    return hints
