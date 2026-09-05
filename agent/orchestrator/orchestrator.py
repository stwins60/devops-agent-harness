"""Orchestrator: drives the task lifecycle and coordinates specialists.

USER REQUEST -> TASK UNDERSTANDING -> CONTEXT DISCOVERY -> INSPECTION -> ROOT CAUSE ANALYSIS -> PLAN
-> RISK ASSESSMENT -> APPROVAL GATE -> IMPLEMENTATION -> VALIDATION -> DOCUMENTATION -> JIRA/PR UPDATE -> FINAL REPORT

Every stage persists task state so an interrupted task can be resumed. The orchestrator stops
whenever an operation is unsafe, ambiguous, unavailable or waiting for a human.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from agent.models import (ApprovalDecision, ApprovalRequest, Diagnosis, Environment, OperatingMode, Plan, RiskLevel, TaskKind, TaskStage,
                          TaskStatus, ValidationResult)
from agent.orchestrator.decider import ModelDecider
from agent.orchestrator.understanding import Understanding, understand
from agent.planners.change_planner import build_change_plan
from agent.rca.engine import EvidenceLog, RootCauseEngine
from agent.reports.render import render_plan
from agent.specialists.base import Investigation, Specialist
from agent.state.store import TaskState

if TYPE_CHECKING:  # pragma: no cover
    from agent.harness import Harness

Progress = Callable[[str], None]
_READ_ONLY_KINDS = (TaskKind.QUESTION, TaskKind.DIAGNOSE, TaskKind.PLAN)
# analyzer name prefix per specialist: the specialists the request was routed to first get precedence when several hypotheses are confirmed
_ANALYZER_PREFIX = {"kubernetes-agent": "k8s", "docker-agent": "docker", "linux-agent": "linux", "cicd-agent": "cicd", "aws-agent": "aws",
                    "terraform-agent": "terraform", "ansible-agent": "ansible", "networking-agent": "net", "observability-agent": "obs",
                    "security-agent": "security", "git-agent": "git", "incident-agent": "incident"}


class Orchestrator:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    # ------------------------------------------------------------------
    def start(self, request: str, *, kind: Optional[TaskKind] = None, task_id: Optional[str] = None, mode: Optional[OperatingMode] = None,
              dry_run: Optional[bool] = None, repo: Optional[Path] = None, progress: Optional[Progress] = None) -> TaskState:
        cfg = self.h.config
        understanding = understand(request, kind)
        kind = understanding.kind
        mode = mode or cfg.mode
        if kind in _READ_ONLY_KINDS and mode not in (OperatingMode.READ_ONLY, OperatingMode.PLAN):
            mode = OperatingMode.PLAN if kind == TaskKind.PLAN else OperatingMode.READ_ONLY
        task = self.h.store.create(request, task_id=task_id or (understanding.ticket if kind == TaskKind.JIRA else None), kind=kind, mode=mode,
                                   environment=cfg.environment, dry_run=cfg.dry_run if dry_run is None else dry_run)
        if repo:
            task.workspace = str(Path(repo).resolve())
        task.context["understanding"] = understanding.to_dict()
        self.h.store.save(task)
        inv = Investigation(task=task, harness=self.h, targets=dict(understanding.targets))
        inv.targets["env_hints"] = understanding.env_hints
        return self._run(task, inv, understanding, progress or (lambda s: None))

    def resume(self, task_id: str, *, progress: Optional[Progress] = None) -> TaskState:
        task = self.h.store.load(task_id)
        ok, why = self.h.store.resumable(task_id)
        p = progress or (lambda s: None)
        if not ok:
            p(f"cannot resume {task_id}: {why}")
            return task
        p(f"resuming {task_id}: {why}")
        u = task.context.get("understanding") or understand(task.request, task.kind).to_dict()
        understanding = Understanding(request=u["request"], kind=TaskKind(u["kind"]), intent=u["intent"], targets=dict(u["targets"]), domains=list(u["domains"]),
                                      env_hints=list(u.get("env_hints", [])), ticket=u.get("ticket"))
        inv = Investigation(task=task, harness=self.h, targets=dict(understanding.targets))
        inv.targets["env_hints"] = understanding.env_hints
        inv.log = EvidenceLog(list(task.evidence))
        for key in ("namespace", "deployment", "pod", "service", "repo", "ticket"):
            val = (task.context.get("targets") or {}).get(key)
            if val:
                inv.set_target(key, val)
        task.status = TaskStatus.RUNNING
        task.note(f"resumed at stage {task.stage.value}")
        return self._run(task, inv, understanding, p, resume=True)

    # ------------------------------------------------------------------
    def _stage(self, task: TaskState, stage: TaskStage, progress: Progress, detail: str = "") -> None:
        task.transition(stage, TaskStatus.RUNNING, detail)
        self.h.audit.stage(task.id, stage.value, "start", detail)
        progress(f"[{stage.value}] {detail}".rstrip())
        self.h.store.save(task)

    def _stop(self, task: TaskState, status: TaskStatus, reason: str, progress: Progress) -> TaskState:
        task.status = status
        task.note(reason)
        self.h.audit.stage(task.id, task.stage.value, status.value, reason)
        progress(f"[{task.stage.value}] {status.value}: {reason}")
        self.h.store.save(task)
        return task

    def _run(self, task: TaskState, inv: Investigation, u: Understanding, progress: Progress, *, resume: bool = False) -> TaskState:
        try:
            return self._lifecycle(task, inv, u, progress, resume)
        except Exception as exc:  # noqa: BLE001 - never lose task state
            task.error(f"{type(exc).__name__}: {exc}")
            self._stop(task, TaskStatus.FAILED, f"unexpected error: {exc}", progress)
            self._document(task, inv, None, progress, final=True)
            raise

    def _lifecycle(self, task: TaskState, inv: Investigation, u: Understanding, progress: Progress, resume: bool) -> TaskState:
        stage = task.stage if resume else TaskStage.RECEIVED
        specialists: list[Specialist] = []

        # 1. understanding -------------------------------------------------
        if stage < TaskStage.CONTEXT or not task.specialists:
            self._stage(task, TaskStage.UNDERSTANDING, progress, u.summary())
            specialists = self._route(task, u)
            task.specialists = [s.name for s in specialists]
            self.h.store.save(task)
        else:
            specialists = [self.h.specialists[n] for n in task.specialists if n in self.h.specialists]

        # 2. context --------------------------------------------------------
        if stage < TaskStage.INSPECTION:
            self._stage(task, TaskStage.CONTEXT, progress)
            self.h.context.collect(inv)
            task.context["targets"] = {k: v for k, v in inv.targets.items() if isinstance(v, (str, int))}
            progress(f"  environment={task.environment.value} ({task.context.get('environment_resolution', {}).get('source')})")

        # 3. inspection -----------------------------------------------------
        if stage < TaskStage.RCA:
            self._stage(task, TaskStage.INSPECTION, progress, ", ".join(s.name for s in specialists))
            self._inspect(task, inv, specialists, progress)
            task.context["targets"] = {k: v for k, v in inv.targets.items() if isinstance(v, (str, int))}
            task.evidence = list(inv.log.items)
            if inv.blocked:
                self._document(task, inv, None, progress, final=True, status=TaskStatus.BLOCKED)
                return self._stop(task, TaskStatus.BLOCKED, inv.blocked, progress)
        if resume and stage >= TaskStage.RCA:
            specialists = [self.h.specialists[n] for n in task.specialists if n in self.h.specialists]

        # 4. root cause analysis -------------------------------------------
        diagnosis = task.diagnosis
        if stage < TaskStage.PLAN or diagnosis is None:
            self._stage(task, TaskStage.RCA, progress)
            diagnosis = self._analyze(task, inv, specialists)
            if not diagnosis.conclusion and self.h.provider.available() and self.h.config.provider != "mock":
                ModelDecider(self.h).investigate(inv, specialists)
                task.evidence = list(inv.log.items)
                diagnosis = self._analyze(task, inv, specialists)
            progress("  " + (f"root cause: {diagnosis.conclusion}" if diagnosis.conclusion else "root cause not confirmed; hypotheses recorded"))

        # 5. plan ----------------------------------------------------------
        plan = task.plan
        if stage < TaskStage.RISK or plan is None:
            self._stage(task, TaskStage.PLAN, progress)
            plan = self._plan(task, inv, specialists, diagnosis)
            task.plan = plan
            self.h.store.save(task)
            progress(f"  plan: {len(plan.changes) if plan else 0} proposed change(s)" if plan else "  no change plan (nothing to change or insufficient evidence)")

        # 6. risk ----------------------------------------------------------
        if stage < TaskStage.APPROVAL:
            self._stage(task, TaskStage.RISK, progress)
            self._assess_risk(task, plan)
            if plan:
                self.h.store.write_artifact(task.id, "plan.md", render_plan(plan))
                (self.h.config.agent_dir / "plan.md").write_text(render_plan(plan), encoding="utf-8")
            progress(f"  risk: {plan.risk_level.value if plan else 'none'}")

        # read-only kinds and modes stop here --------------------------------
        if task.kind in _READ_ONLY_KINDS or task.mode in (OperatingMode.READ_ONLY, OperatingMode.PLAN) or not plan or not plan.changes:
            reason = "read-only/plan task completed" if task.kind in _READ_ONLY_KINDS or task.mode in (OperatingMode.READ_ONLY, OperatingMode.PLAN) else \
                "no executable change proposed" if not plan or not plan.changes else "done"
            task.transition(TaskStage.REPORT, TaskStatus.COMPLETED, reason)
            self._document(task, inv, plan, progress, final=True)
            task.transition(TaskStage.DONE, TaskStatus.COMPLETED, reason)
            self.h.store.save(task)
            progress(f"[done] {reason}")
            return task

        # 7. approval gate ---------------------------------------------------
        if stage < TaskStage.IMPLEMENTATION and not plan.approved:
            self._stage(task, TaskStage.APPROVAL, progress)
            if not self._approve_plan(task, plan, progress):
                self._document(task, inv, plan, progress, final=True)
                return task

        # 8. implementation --------------------------------------------------
        owner = self._owner(task, specialists, plan)
        if stage < TaskStage.VALIDATION:
            self._stage(task, TaskStage.IMPLEMENTATION, progress, f"by {owner.name if owner else 'orchestrator'}")
            (owner or Specialist(self.h)).implement(inv, plan)
            task.changes = plan.changes
            self.h.store.save(task)
            applied = [c for c in plan.changes if c.applied]
            progress(f"  applied {len(applied)}/{len(plan.changes)} change(s)" + (" (dry-run)" if task.dry_run else ""))
            if inv.blocked:
                status = TaskStatus.DENIED if "denied" in inv.blocked else TaskStatus.BLOCKED
                self._document(task, inv, plan, progress, final=True, status=status)
                return self._stop(task, status, inv.blocked, progress)
            if not applied and not task.dry_run:
                failed = [c for c in plan.changes if c.result and not c.result.get("ok")]
                self._document(task, inv, plan, progress, final=True, status=TaskStatus.FAILED)
                return self._stop(task, TaskStatus.FAILED, f"no change could be applied: {failed[0].result.get('error') if failed else 'nothing to apply'}", progress)

        # 9. validation ------------------------------------------------------
        if stage < TaskStage.DOCUMENTATION:
            self._stage(task, TaskStage.VALIDATION, progress)
            results = self._validate(task, inv, plan, owner)
            task.validation = results
            self.h.store.save(task)
            for r in results:
                progress(f"  {r.name}: {r.label}{' - ' + r.detail if r.detail else ''}")
            failures = [r for r in results if not r.passed and not r.skipped]
            if failures and not task.dry_run:
                rolled = self.h.executor.rollback_all(task)
                for c in plan.changes:
                    if c.applied and any(e.tool == c.tool and e.ok for e in rolled):
                        c.applied = False
                task.note(f"validation failed ({failures[0].name}); rolled back {sum(1 for e in rolled if e.ok)}/{len(rolled)} mutation(s)")
                self._document(task, inv, plan, progress, final=True, rollback_text=self.h.executor.rollback_plan(task).render(), status=TaskStatus.FAILED)
                return self._stop(task, TaskStatus.FAILED, f"validation failed: {failures[0].name} - {failures[0].detail}", progress)

        # 10. documentation ------------------------------------------------
        if stage < TaskStage.UPDATE:
            self._stage(task, TaskStage.DOCUMENTATION, progress)
            self._document(task, inv, plan, progress, final=False)

        # 11. external update (git/PR/Jira) ------------------------------------
        if stage < TaskStage.REPORT:
            self._stage(task, TaskStage.UPDATE, progress)
            self._update_external(task, inv, plan, diagnosis, progress)
            if inv.blocked:
                status = TaskStatus.DENIED if "denied" in inv.blocked else TaskStatus.BLOCKED
                self._document(task, inv, plan, progress, final=True, status=status)
                return self._stop(task, status, inv.blocked, progress)

        # 12. final report ---------------------------------------------------
        self._stage(task, TaskStage.REPORT, progress)
        task.status = TaskStatus.COMPLETED
        self._document(task, inv, plan, progress, final=True)
        task.transition(TaskStage.DONE, TaskStatus.COMPLETED, "task completed")
        self.h.store.save(task)
        progress("[done] task completed")
        return task

    # ------------------------------------------------------------------
    def _route(self, task: TaskState, u: Understanding) -> list[Specialist]:
        scored = []
        for s in self.h.specialists.values():
            if s.name == "documentation-agent":
                continue
            sc = s.score(u.request, u.kind, u.targets)
            if sc > 0:
                scored.append((sc, s))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        chosen = [s for _, s in scored[:4]]
        if u.kind == TaskKind.JIRA:
            chosen = [self.h.specialists["jira-agent"]] + [s for s in chosen if s.name != "jira-agent"]
        if u.kind == TaskKind.INCIDENT:
            chosen = [self.h.specialists["incident-agent"]]  # incident agent fans out to the domain specialists itself
        if not chosen:
            chosen = [self.h.specialists["kubernetes-agent"]] if "kubernetes" in u.domains else [self.h.specialists["git-agent"]]
        if u.kind == TaskKind.PLAN and "kubernetes" in u.domains and any(w in u.request.lower() for w in ("node", "upgrade", "cluster")):
            for extra in ("aws-agent", "terraform-agent"):
                if self.h.specialists[extra] not in chosen:
                    chosen.append(self.h.specialists[extra])
        task.note("routed to " + ", ".join(s.name for s in chosen))
        return chosen

    def _inspect(self, task: TaskState, inv: Investigation, specialists: list[Specialist], progress: Progress) -> None:
        done: set[str] = set()
        queue = list(specialists)
        while queue:
            sp = queue.pop(0)
            if sp.name in done:
                continue
            done.add(sp.name)
            progress(f"  {sp.name} investigating")
            sp.investigate(inv)
            task.evidence = list(inv.log.items)
            self.h.store.save(task)
            if inv.blocked:
                return
            if sp.name == "jira-agent":
                ticket_text = inv.targets.get("ticket_text", "")
                if ticket_text:
                    extra = [s for s in self.h.specialists.values() if s.name not in done and s.name not in ("documentation-agent", "incident-agent")
                             and s.score(ticket_text, task.kind, inv.targets) >= 2]
                    extra.sort(key=lambda s: -s.score(ticket_text, task.kind, inv.targets))
                    for s in extra[:3]:
                        if s not in queue and s.name not in task.specialists:
                            task.specialists.append(s.name)
                            queue.append(s)
                    if inv.task.workspace and "git-agent" not in done and self.h.specialists["git-agent"] not in queue:
                        task.specialists.append("git-agent")
                        queue.append(self.h.specialists["git-agent"])
            if len(task.tool_calls) >= self.h.config.limits.max_tool_calls:
                task.error("tool call budget exhausted during inspection")
                return

    def _analyze(self, task: TaskState, inv: Investigation, specialists: list[Specialist]) -> Diagnosis:
        engine = RootCauseEngine()
        names = set(task.specialists)
        for sp in self.h.specialists.values():
            if sp.name in names or sp.name in ("kubernetes-agent", "observability-agent", "networking-agent") and task.kind == TaskKind.INCIDENT:
                for name, fn in sp.analyzers():
                    engine.register(name, fn)
        problem = inv.targets.get("ticket_text", "").split("\n")[0] or task.request
        prefixes = [_ANALYZER_PREFIX[n] for n in task.specialists if n in _ANALYZER_PREFIX]
        if task.kind == TaskKind.INCIDENT:
            prefixes = ["k8s", "obs", "net", "linux", "docker"]
        diagnosis = engine.analyze(problem, inv.log, specialist=",".join(task.specialists), prefer_prefixes=prefixes)
        task.diagnosis = diagnosis
        task.evidence = list(inv.log.items)
        self.h.store.write_artifact(task.id, "evidence.md", RootCauseEngine.render(diagnosis))
        self.h.store.save(task)
        return diagnosis

    def _plan(self, task: TaskState, inv: Investigation, specialists: list[Specialist], diagnosis: Diagnosis) -> Optional[Plan]:
        proposals: list[Plan] = []
        for sp in [self.h.specialists[n] for n in task.specialists if n in self.h.specialists]:
            p = sp.propose(inv, diagnosis)
            if p:
                p.title = p.title or task.request[:80]
                proposals.append(p)
                task.checkpoint.setdefault("proposals", []).append({"specialist": sp.name, "title": p.title, "changes": len(p.changes)})
        if task.kind == TaskKind.PLAN:
            return build_change_plan(inv, diagnosis, proposals)
        chosen = next((p for p in proposals if p.changes), proposals[0] if proposals else None)
        if chosen is None and self.h.provider.available() and self.h.config.provider != "mock" and task.kind in (TaskKind.JIRA, TaskKind.FIX, TaskKind.EXECUTE):
            chosen = ModelDecider(self.h).propose(inv, diagnosis)
        if chosen is None and task.kind in (TaskKind.JIRA, TaskKind.FIX):
            chosen = Plan(task_id=task.id, title=task.request[:80], problem=task.request, root_cause=diagnosis.conclusion or "not confirmed",
                          evidence=[f.statement for f in diagnosis.facts][:10],
                          risks=["no specialist could derive a concrete change from the evidence; a model provider or a human must draft the implementation"])
        if chosen:
            chosen.task_id = task.id
            task.checkpoint["plan_owner"] = next((d["specialist"] for d in task.checkpoint.get("proposals", []) if d["title"] == chosen.title), None)
            task.changes = chosen.changes  # proposed (not applied) until the implementation stage marks them
        return chosen

    def _assess_risk(self, task: TaskState, plan: Optional[Plan]) -> None:
        if not plan:
            return
        for c in plan.changes:
            if c.risk.rank > plan.risk_level.rank:
                plan.risk_level = c.risk
            if c.environment is None:
                c.environment = task.environment.value
        if task.environment in (Environment.PRODUCTION, Environment.UNKNOWN) and any(c.kind in ("infrastructure", "command") for c in plan.changes):
            if plan.risk_level.rank < RiskLevel.HIGH.rank:
                plan.risk_level = RiskLevel.HIGH
            plan.risks.append(f"target environment is {task.environment.value}: every mutation requires explicit approval")
        perms = sorted({c.permission.name for c in plan.changes})
        plan.required_permissions = sorted(set(plan.required_permissions) | set(perms))
        self.h.store.save(task)

    def _approve_plan(self, task: TaskState, plan: Plan, progress: Progress) -> bool:
        if task.dry_run:
            task.note("dry-run: plan approval skipped; nothing will be executed")
            return True
        req = ApprovalRequest(operation=f"execute plan for {task.id}", description=plan.title, environment=task.environment.value, risk=plan.risk_level,
                              resources=[c.target for c in plan.changes], expected_impact="; ".join(c.description for c in plan.changes)[:400],
                              rollback="; ".join(plan.rollback) or "see per-change rollback", plan=render_plan(plan), tool="plan",
                              diff="\n".join(c.diff for c in plan.changes if c.diff) or None)
        explicit = task.environment in (Environment.PRODUCTION, Environment.UNKNOWN) and any(c.kind in ("infrastructure", "command") for c in plan.changes)
        task.status = TaskStatus.WAITING_APPROVAL
        self.h.store.save(task)
        outcome = self.h.approvals.ask(req, explicit=explicit)
        task.approvals.append(outcome.record(req))
        self.h.audit.approval(task=task.id, operation=req.operation, decision=outcome.decision.value, decided_by=outcome.decided_by, risk=plan.risk_level.value,
                              environment=task.environment.value)
        if outcome.decision == ApprovalDecision.APPROVE:
            plan.approved = True
            plan.approval_note = f"{outcome.decided_by}: {outcome.note}"
            task.status = TaskStatus.RUNNING
            self.h.store.save(task)
            progress("  plan approved")
            return True
        plan.approval_note = f"{outcome.decided_by}: {outcome.note}"
        human = outcome.decided_by not in ("auto-deny", "auto-approve", "allowlist")
        status = TaskStatus.DENIED if outcome.decision == ApprovalDecision.DENY and human else TaskStatus.PAUSED
        hint = "--approve-all (explicit confirmation) or run interactively" if "explicit" in outcome.note else "--yes"
        reason = "plan denied by approver" if status == TaskStatus.DENIED else f"waiting for approval ({outcome.note}); resume with: devops-agent execute {task.id} {hint}"
        self._stop(task, status, reason, progress)
        return False

    def _owner(self, task: TaskState, specialists: list[Specialist], plan: Plan) -> Optional[Specialist]:
        name = task.checkpoint.get("plan_owner")
        if name and name in self.h.specialists:
            return self.h.specialists[name]
        for n in task.specialists:
            if n in self.h.specialists and n not in ("jira-agent", "git-agent"):
                return self.h.specialists[n]
        return specialists[0] if specialists else None

    def _validate(self, task: TaskState, inv: Investigation, plan: Plan, owner: Optional[Specialist]) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        if owner:
            results.extend(owner.validate(inv, plan))
        if any(c.kind == "file" for c in plan.changes) and task.workspace:
            results.extend(self.h.specialists["git-agent"].validate(inv, plan))
            results.extend(self._run_project_tests(task, inv))
            sec = self.h.specialists["security-agent"].validate(inv, plan)
            results.extend(sec)
            task.checkpoint["security"] = [f"{r.name}: {r.label}" + (f" ({r.detail})" if r.detail else "") for r in sec]
        if not results:
            results.append(ValidationResult("validation", False, "no validation could be performed for this change", skipped=task.dry_run))
        for rb in inv.runbooks_used:
            runbook = self.h.runbooks.get(rb)
            if runbook and runbook.validation:
                task.note(f"runbook '{rb}' validation steps: " + "; ".join(s.description for s in runbook.validation))
        return results

    def _run_project_tests(self, task: TaskState, inv: Investigation) -> list[ValidationResult]:
        ws = Path(task.workspace)
        if not (ws / "tests").exists():
            return [ValidationResult("project tests", True, "no tests directory", skipped=True)]
        if task.dry_run:
            return [ValidationResult("project tests", True, "dry-run: tests not executed", skipped=True)]
        cmd = f'"{sys.executable}" -m pytest -p no:cacheprovider --no-header tests'
        res = self.h.executor.run("shell_run", {"command": cmd, "cwd": str(ws), "timeout": 300}, task, agent="orchestrator", purpose="run project tests")
        if res.failure_kind == "policy":
            return [ValidationResult("project tests", False, res.error or "blocked")]
        if self.h.config.mock:
            # the mock linux backend does not run real commands; run the tests for real in the workspace
            try:
                proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", "tests"], cwd=str(ws), capture_output=True, text=True, timeout=300)
                return [ValidationResult("project tests (pytest)", proc.returncode == 0, _pytest_summary(proc.stdout + proc.stderr, proc.returncode))]
            except (subprocess.TimeoutExpired, OSError) as exc:
                return [ValidationResult("project tests (pytest)", False, str(exc)[:200])]
        rc = (res.output or {}).get("returncode", 1) if isinstance(res.output, dict) else 1
        ok = res.ok and rc == 0
        text = ((res.output or {}).get("stdout", "") + (res.output or {}).get("stderr", "")) if isinstance(res.output, dict) else (res.error or "")
        return [ValidationResult("project tests (pytest)", ok, _pytest_summary(text, rc))]

    def _document(self, task: TaskState, inv: Investigation, plan: Optional[Plan], progress: Progress, *, final: bool, rollback_text: Optional[str] = None,
                  status: Optional[TaskStatus] = None) -> None:
        if status is not None:
            task.status = status  # the report must show the outcome, so set it before rendering
        if final and task.diagnosis is None and inv.log.items:
            self._analyze(task, inv, [])  # a stopped task still gets hypotheses from whatever evidence exists
        doc = self.h.specialists["documentation-agent"]
        incident = None
        if task.kind == TaskKind.INCIDENT:
            incident = self.h.specialists["incident-agent"].artifacts(inv, task.diagnosis)
            task.links.incident = task.id
        deployment = None
        applied_infra = [c for c in task.changes if c.applied and c.kind == "infrastructure"]
        if applied_infra:
            deployment = "; ".join(c.description for c in applied_infra)
        elif task.kind in (TaskKind.JIRA, TaskKind.FIX) and task.links.pull_request:
            deployment = "Not deployed by this task: the fix is in a pull request awaiting review (GitOps/CI deploys after merge)."
        report = doc.write_artifacts(inv, security=task.checkpoint.get("security"), deployment=deployment,
                                     rollback_text=rollback_text or (self.h.executor.rollback_plan(task).render() if task.checkpoint.get("rollback") else None), incident=incident)
        if final:
            doc.remember_outcome(inv, task.diagnosis, plan)
            self.h.audit.flush_metrics(task.id)
            task.metrics = self.h.audit.metrics.snapshot()
        self.h.store.save(task)
        return None

    def _update_external(self, task: TaskState, inv: Investigation, plan: Plan, diagnosis: Diagnosis, progress: Progress) -> None:
        file_changes = [c for c in plan.changes if c.kind == "file" and (c.applied or task.dry_run)]
        outcome = "changes-applied"
        if file_changes and task.workspace:
            git = self.h.specialists["git-agent"]
            ticket = task.links.jira_issue or inv.target("ticket")
            body = self._pr_body(task, plan, diagnosis)
            delivered = git.deliver(inv, plan, ticket=ticket, title=plan.title[:70], body=body)
            if delivered.get("error"):
                task.error(f"delivery: {delivered['error']}")
                progress(f"  delivery stopped: {delivered['error']}")
                if any(k in delivered["error"] for k in ("approval denied", "blocked by policy")):
                    inv.blocked = f"delivery stopped: {delivered['error']}"
                    return
                outcome = "changes-applied-not-delivered"
            else:
                outcome = "pr-opened"
                progress(f"  branch {delivered['branch']} commit {delivered['commit']} PR {delivered['pr']}")
        if task.links.jira_issue:
            jira = self.h.specialists["jira-agent"]
            lines = [f"Root cause: {diagnosis.conclusion or 'not confirmed'}", "Validation: " + ", ".join(f"{r.name}={r.label}" for r in task.validation)]
            if task.links.pull_request:
                lines.append(f"Pull request: {task.links.pull_request}")
            if task.links.branch:
                lines.append(f"Branch: {task.links.branch}")
            done = jira.update_ticket(inv, plan, outcome=outcome, summary_lines=lines)
            progress(f"  jira: comment={'yes' if done['comment'] else 'no'} transition={done['transition'] or 'none'}")

    @staticmethod
    def _pr_body(task: TaskState, plan: Plan, diagnosis: Diagnosis) -> str:
        lines = [f"## Summary\n\n{plan.title}", f"\n## Root cause\n\n{diagnosis.conclusion or 'see evidence'}", "\n## Evidence\n"]
        lines += [f"- {f.statement}" for f in diagnosis.facts[:8]]
        lines += ["\n## Changes\n"] + [f"- {c.description}" for c in plan.changes]
        lines += ["\n## Validation\n"] + [f"- {r.name}: {r.label}" + (f" ({r.detail})" if r.detail else "") for r in task.validation]
        lines += ["\n## Rollback\n"] + [f"- {r}" for r in plan.rollback] if plan.rollback else ["\n## Rollback\n\n- git revert"]
        lines += [f"\nJira: {task.links.jira_issue}" if task.links.jira_issue else "", "\nGenerated by devops-agent-harness (evidence-backed; human review required)."]
        return "\n".join(lines)


def _pytest_summary(text: str, returncode: int) -> str:
    import re

    for line in reversed((text or "").strip().splitlines()):
        if re.search(r"\d+ (passed|failed|error)|no tests ran|error", line, re.I):
            return line.strip()[:200]
    lines = (text or "").strip().splitlines()
    return (lines[-1].strip()[:200] if lines else f"exit {returncode}")
