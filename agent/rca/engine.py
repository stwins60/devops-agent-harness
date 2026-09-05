"""Root cause analysis engine.

The engine keeps a strict separation between FACT (observed via a tool),
HYPOTHESIS (candidate explanation with a validation step), INFERENCE
(derived from facts) and RECOMMENDATION. Specialists register *analyzers*:
pure functions that look at collected facts and emit hypotheses, optionally
confirming them when the validating evidence is already present.

A conclusion is only produced when at least one hypothesis is confirmed by
evidence; otherwise the report says so explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.models import Diagnosis, Evidence, EvidenceKind, Hypothesis

Analyzer = Callable[["EvidenceLog"], list[Hypothesis]]


@dataclass
class EvidenceLog:
    """Ordered evidence with convenient lookups by source/tag."""

    items: list[Evidence] = field(default_factory=list)

    def fact(self, statement: str, source: str = "", **data: Any) -> Evidence:
        for existing in self.items:
            if existing.kind == EvidenceKind.FACT and existing.statement == statement:
                existing.data.update(data)  # identical fact observed again: keep one copy, merge data
                return existing
        ev = Evidence(EvidenceKind.FACT, statement, source=source, data=data)
        self.items.append(ev)
        return ev

    def inference(self, statement: str, source: str = "", confidence: float = 0.7, **data: Any) -> Evidence:
        ev = Evidence(EvidenceKind.INFERENCE, statement, source=source, data=data, confidence=confidence)
        self.items.append(ev)
        return ev

    def recommendation(self, statement: str, source: str = "", **data: Any) -> Evidence:
        ev = Evidence(EvidenceKind.RECOMMENDATION, statement, source=source, data=data)
        self.items.append(ev)
        return ev

    def facts(self) -> list[Evidence]:
        return [e for e in self.items if e.kind == EvidenceKind.FACT]

    def by_source(self, prefix: str) -> list[Evidence]:
        return [e for e in self.items if e.source.startswith(prefix)]

    def find(self, key: str, value: Any = None) -> list[Evidence]:
        out = []
        for e in self.items:
            if key in e.data and (value is None or e.data[key] == value):
                out.append(e)
        return out

    def get(self, key: str, default: Any = None) -> Any:
        for e in reversed(self.items):
            if key in e.data:
                return e.data[key]
        return default

    def has(self, key: str, value: Any = None) -> bool:
        return bool(self.find(key, value))

    def any_statement(self, *needles: str) -> bool:
        low = [n.lower() for n in needles]
        return any(any(n in e.statement.lower() for n in low) for e in self.items)

    def extend(self, other: "EvidenceLog") -> None:
        self.items.extend(other.items)

    def render(self) -> str:
        lines = []
        for e in self.items:
            src = f"  (source: {e.source})" if e.source else ""
            lines.append(f"{e.kind.value}:\n{e.statement}{src}\n")
        return "\n".join(lines)


class RootCauseEngine:
    def __init__(self) -> None:
        self.analyzers: list[tuple[str, Analyzer]] = []

    def register(self, name: str, analyzer: Analyzer) -> None:
        self.analyzers.append((name, analyzer))

    def analyze(self, problem: str, log: EvidenceLog, *, specialist: str = "", extra_hypotheses: Optional[list[Hypothesis]] = None,
                prefer_prefixes: Optional[list[str]] = None) -> Diagnosis:
        hypotheses: list[Hypothesis] = list(extra_hypotheses or [])
        for name, analyzer in self.analyzers:
            try:
                for h in analyzer(log) or []:
                    h.supporting = h.supporting or [name]
                    hypotheses.append(h)
            except Exception as exc:  # analyzers must never take the harness down
                hypotheses.append(Hypothesis(statement=f"analyzer '{name}' failed: {exc}", validation="fix analyzer",
                                             status="inconclusive", confidence=0.0))
        hypotheses = _dedupe(hypotheses)
        confirmed = [h for h in hypotheses if h.status == "confirmed"]

        def rank(h: Hypothesis) -> tuple[int, float]:
            prefix = (h.supporting[0].split(".")[0] if h.supporting else "")
            order = prefer_prefixes.index(prefix) if prefer_prefixes and prefix in prefer_prefixes else len(prefer_prefixes or [])
            return (order, -h.confidence)

        confirmed.sort(key=rank)
        diag = Diagnosis(problem=problem, facts=log.facts(), hypotheses=hypotheses, specialist=specialist)
        if confirmed:
            best = confirmed[0]
            diag.conclusion = best.statement
            diag.confidence = best.confidence
        elif hypotheses:
            open_h = [h for h in hypotheses if h.status != "rejected"]
            if open_h:
                open_h.sort(key=lambda h: h.confidence, reverse=True)
                diag.conclusion = None
                diag.confidence = open_h[0].confidence
        recs = [e.statement for e in log.items if e.kind == EvidenceKind.RECOMMENDATION]
        diag.recommendations = list(dict.fromkeys(recs))
        return diag

    @staticmethod
    def render(diag: Diagnosis) -> str:
        lines = [f"Problem:\n{diag.problem}", ""]
        for f in diag.facts:
            src = f"  (source: {f.source})" if f.source else ""
            lines.append(f"FACT:\n{f.statement}{src}\n")
        for h in diag.hypotheses:
            lines.append(f"HYPOTHESIS ({h.status}, confidence {h.confidence:.0%}):\n{h.statement}\nVALIDATION:\n{h.validation}\n")
        if diag.conclusion:
            lines.append(f"CONCLUSION:\nConfirmed - {diag.conclusion} (confidence {diag.confidence:.0%})")
        else:
            lines.append("CONCLUSION:\nNo hypothesis is confirmed by the collected evidence. "
                         "The most likely candidates above still require the listed validation steps.")
        if diag.recommendations:
            lines.append("")
            lines.append("RECOMMENDATION:")
            lines.extend(f"- {r}" for r in diag.recommendations)
        return "\n".join(lines)


def _dedupe(hyps: list[Hypothesis]) -> list[Hypothesis]:
    seen: dict[str, Hypothesis] = {}
    for h in hyps:
        key = h.statement.strip().lower()
        if key in seen:
            existing = seen[key]
            if h.status == "confirmed" and existing.status != "confirmed":
                seen[key] = h
            elif h.confidence > existing.confidence and h.status == existing.status:
                seen[key] = h
        else:
            seen[key] = h
    return list(seen.values())
