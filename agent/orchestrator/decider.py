"""Model-driven decision loop (OBSERVE -> THINK -> PLAN -> ACT -> OBSERVE -> VALIDATE -> REPLAN).

Used only when the rule-based specialists cannot conclude. The model proposes
tool calls and structured conclusions; the executor (policy + approval +
audit) still decides what actually runs. The loop is bounded by the configured
iteration and tool-call limits and stops on repeated identical requests.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from agent.audit.redaction import redact, redact_text
from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel
from agent.providers.base import ModelMessage, ModelRequest, ProviderError
from agent.specialists.base import Investigation, Specialist

if TYPE_CHECKING:  # pragma: no cover
    from agent.harness import Harness

SYSTEM_PROMPT = """You are the reasoning engine of a DevOps agent harness. You never execute anything yourself:
you request tools and the harness enforces policy, approvals and audit logging outside of you.
Rules: never fabricate tool output; distinguish FACT / HYPOTHESIS / INFERENCE / RECOMMENDATION; prefer read-only tools;
do not claim a root cause without supporting evidence; never include credentials.
Respond ONLY with JSON. To call a tool: {"action":"tool","tool":"<name>","args":{...},"why":"..."}.
To finish: {"action":"complete","summary":"...","root_cause":"..."|null,"confidence":0.0-1.0,
"facts":["..."],"hypotheses":[{"statement":"...","validation":"...","status":"confirmed|unvalidated|rejected","confidence":0.0-1.0}],
"recommendations":["..."],"changes":[{"description":"...","path":"...","old":"...","new":"..."}]}"""


class ModelDecider:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    def _tools(self, max_permission: PermissionLevel) -> list[dict[str, Any]]:
        return self.h.registry.model_tool_definitions(max_permission=max_permission)

    def _observe(self, inv: Investigation) -> str:
        parts = [f"REQUEST: {inv.task.request}", f"CONTEXT:\n{inv.task.context.get('summary', '')[:3000]}", "EVIDENCE SO FAR:"]
        parts += [f"- {e.kind.value}: {e.statement[:300]}" for e in inv.log.items[-40:]]
        return "\n".join(parts)

    def _loop(self, inv: Investigation, goal: str, max_permission: PermissionLevel) -> Optional[dict[str, Any]]:
        task = inv.task
        messages = [ModelMessage("user", f"{goal}\n\n{self._observe(inv)}")]
        seen: dict[str, int] = {}
        for iteration in range(self.h.config.limits.max_iterations):
            request = ModelRequest(system=SYSTEM_PROMPT, messages=messages, tools=self._tools(max_permission), json_mode=True)
            try:
                response = self.h.provider.complete(request)
            except ProviderError as exc:
                task.error(f"model provider failed: {exc}")
                return None
            self.h.audit.model_usage(response.provider, response.model, response.prompt_tokens, response.completion_tokens,
                                     float((response.raw or {}).get("duration", 0.0)) if isinstance(response.raw, dict) else 0.0)
            decision = response.parsed_json() or {}
            if response.tool_calls and not decision.get("action"):
                tc = response.tool_calls[0]
                decision = {"action": "tool", "tool": tc.name, "args": tc.arguments}
            action = decision.get("action")
            if action == "tool":
                name, args = str(decision.get("tool")), dict(decision.get("args") or {})
                key = name + json.dumps(args, sort_keys=True, default=str)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > self.h.config.limits.max_repeated_calls:
                    task.error("model requested the same tool call repeatedly; stopping the loop")
                    return None
                result = self.h.executor.run(name, args, task, agent="model-decider", purpose=str(decision.get("why", "model requested")))
                payload = redact(result.output) if result.ok else {"error": result.error, "advice": result.advice, "kind": result.failure_kind}
                text = json.dumps(payload, default=str)[:6000]
                if result.ok:
                    inv.log.fact(f"{name}({', '.join(f'{k}={str(v)[:30]}' for k, v in args.items())}) -> {redact_text(text)[:300]}", source=f"{name}(model)")
                messages.append(ModelMessage("assistant", response.text or json.dumps(decision)))
                messages.append(ModelMessage("tool", text, name=name, tool_call_id=response.tool_calls[0].id if response.tool_calls else None))
                continue
            if action == "complete":
                return decision
            messages.append(ModelMessage("assistant", response.text))
            messages.append(ModelMessage("user", "Respond with valid JSON: either a tool call or a completion object."))
        task.error("model loop reached the iteration limit without completing")
        return None

    def investigate(self, inv: Investigation, specialists: list[Specialist]) -> Optional[Diagnosis]:
        decision = self._loop(inv, "Investigate the request using read-only tools until you can state facts and a validated root cause.", PermissionLevel.ANALYZE)
        if not decision:
            return None
        for f in decision.get("facts") or []:
            inv.log.fact(str(f), source="model (from tool results)", confidence=0.7)
        for r in decision.get("recommendations") or []:
            inv.log.recommendation(str(r), source="model")
        hyps = [Hypothesis(statement=str(h.get("statement", "")), validation=str(h.get("validation", "")), status=str(h.get("status", "unvalidated")),
                           confidence=float(h.get("confidence", 0.5))) for h in (decision.get("hypotheses") or []) if isinstance(h, dict)]
        inv.task.checkpoint["model_hypotheses"] = [h.to_dict() for h in hyps]
        # the model may not mark a hypothesis confirmed without tool-derived facts
        if not inv.log.facts():
            for h in hyps:
                h.status = "unvalidated"
        diag = Diagnosis(problem=inv.task.request, facts=inv.log.facts(), hypotheses=hyps, conclusion=None, specialist="model-decider")
        confirmed = [h for h in hyps if h.status == "confirmed"]
        if confirmed and inv.log.facts():
            diag.conclusion = confirmed[0].statement
            diag.confidence = confirmed[0].confidence
        inv.task.checkpoint["model_summary"] = str(decision.get("summary", ""))[:1000]
        return diag

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        decision = self._loop(inv, "Propose the minimal, reversible file changes that fix the confirmed root cause. Use fs_read/fs_search to inspect files first.",
                              PermissionLevel.ANALYZE)
        if not decision or not decision.get("changes"):
            return None
        plan = Plan(task_id=inv.task.id, title=str(decision.get("summary", inv.task.request))[:80], problem=inv.task.request, root_cause=diagnosis.conclusion or "",
                    evidence=[f.statement for f in diagnosis.facts][:10], rollback=["git revert the fix commit"], validation=["project tests", "security scan"])
        for c in decision["changes"]:
            if not isinstance(c, dict) or not c.get("path") or c.get("old") is None:
                continue
            plan.changes.append(ProposedChange(description=str(c.get("description", "model-proposed change")), kind="file", target=str(c["path"]), tool="fs_replace",
                                               args={"path": str(c["path"]), "old": str(c["old"]), "new": str(c.get("new", ""))}, risk=RiskLevel.LOW,
                                               permission=PermissionLevel.MODIFY, rollback=f"restore previous content of {c['path']}"))
            plan.files.append(str(c["path"]))
        return plan if plan.changes else None
