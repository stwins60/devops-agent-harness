"""Jira specialist: reads tickets, derives targets, updates the ticket at the end of the workflow."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Optional

from agent.audit.redaction import redact_text
from agent.models import Plan
from agent.specialists.base import Investigation, Specialist


class JiraSpecialist(Specialist):
    name = "jira-agent"
    description = "Reads Jira issues, extracts requirements and targets, and keeps the ticket updated (comments, status, labels, worklogs)."
    domains = ["jira"]
    keywords = ["jira", "ticket", "issue", "story", "epic", "sprint"]

    def investigate(self, inv: Investigation) -> None:
        key = inv.target("ticket")
        if not key:
            inv.log.fact("No Jira issue key found in the request.", source="jira-agent")
            return
        res = self.call(inv, "jira_get_issue", {"key": key}, purpose="read the ticket")
        if not res.ok:
            if res.failure_kind in ("network", "auth", "permission", "unavailable"):
                inv.blocked = f"Jira unavailable: {res.error}"
            return
        issue: dict[str, Any] = res.output
        inv.task.links.jira_issue = issue.get("key")
        inv.task.context["jira"] = {k: issue.get(k) for k in ("key", "summary", "status", "priority", "labels", "components", "assignee", "issuetype")}
        desc = str(issue.get("description") or "")
        inv.log.fact(f"Jira {issue['key']} [{issue.get('issuetype')}, {issue.get('priority')}, {issue.get('status')}]: {issue.get('summary')}", source=f"jira_get_issue({key})",
                     jira_key=issue["key"], jira_status=issue.get("status"))
        if desc:
            inv.log.fact("Ticket description: " + redact_text(desc.strip())[:600].replace("\n", " "), source=f"jira_get_issue({key})")
        ac = issue.get("acceptance_criteria") or []
        if ac:
            inv.log.fact("Acceptance criteria: " + "; ".join(ac), source=f"jira_get_issue({key})", acceptance_criteria=ac)
        for c in (issue.get("comments") or [])[-3:]:
            inv.log.fact(f"Comment by {c.get('author')}: {str(c.get('body'))[:200]}", source=f"jira_get_issue({key})")
        if issue.get("links"):
            inv.log.fact("Linked issues: " + ", ".join(f"{l.get('type')} {l.get('key')}" for l in issue["links"]), source=f"jira_get_issue({key})")
        # targets from ticket text ------------------------------------------------
        text = f"{issue.get('summary', '')}\n{desc}"
        for pat, key_name in ((r"repository:\s*([\w./\-]+)", "repo_hint"), (r"service:\s*([\w\-]+)", "service"), (r"namespace:\s*([\w\-]+)", "namespace"),
                              (r"cluster:\s*([\w\-]+)", "cluster"), (r"deployment:\s*([\w\-]+)", "deployment")):
            m = re.search(pat, text, re.I)
            if m:
                inv.set_target(key_name, m.group(1))
        if issue.get("custom_repository"):
            inv.set_target("repo_hint", issue["custom_repository"])
        if inv.target("service") and not inv.target("deployment"):
            inv.set_target("deployment", inv.target("service"))
        from agent.orchestrator.understanding import detect_domains

        for d in detect_domains(text):
            if d not in inv.targets.setdefault("domains", []):
                inv.targets["domains"].append(d)
        inv.targets["ticket_text"] = text
        self._prepare_workspace(inv)

    def _prepare_workspace(self, inv: Investigation) -> None:
        """Locate the repository named by the ticket and stage it as the task workspace."""
        if inv.task.workspace:
            return
        hint = inv.target("repo_hint") or self.h.config.default_repo_path
        if not hint:
            inv.log.fact("Ticket does not name a repository; repository inspection requires --repo.", source="jira-agent")
            return
        candidates = [Path(hint), self.h.config.project_root / hint, Path(__file__).resolve().parents[2] / hint]
        src = next((c for c in candidates if c.exists() and c.is_dir()), None)
        if src is None:
            inv.log.fact(f"Repository '{hint}' named by the ticket is not available locally; clone it or pass --repo.", source="jira-agent", repo_missing=hint)
            inv.notes.append(f"repository '{hint}' not found locally")
            return
        ws = self.h.store.workspace_dir(inv.task.id)
        if not any(ws.iterdir()):
            shutil.copytree(src, ws, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"))
            inv.notes.append(f"copied repository {src} into task workspace {ws}")
        inv.task.workspace = str(ws)
        inv.set_target("repo", str(ws))
        inv.log.fact(f"Repository '{hint}' staged in task workspace ({sum(1 for _ in ws.rglob('*') if _.is_file())} files).", source="jira-agent", repo=str(ws))

    # ------------------------------------------------------------------
    def update_ticket(self, inv: Investigation, plan: Optional[Plan], *, outcome: str, summary_lines: list[str]) -> dict[str, Any]:
        """Comment, label and transition the ticket according to the task outcome. Returns what was done."""
        key = inv.task.links.jira_issue or inv.target("ticket")
        done: dict[str, Any] = {"comment": False, "transition": None, "labels": False}
        if not key:
            return done
        body = "\n".join([f"devops-agent update ({outcome}):", *summary_lines])
        res = self.call(inv, "jira_add_comment", {"key": key, "body": body}, purpose="post progress comment to the ticket")
        done["comment"] = res.ok or res.dry_run
        labels = self.call(inv, "jira_add_labels", {"key": key, "labels": ["devops-agent"]}, purpose="label the ticket")
        done["labels"] = labels.ok or labels.dry_run
        target_status = "In Review" if outcome == "pr-opened" else "In Progress"
        trans = self.call(inv, "jira_get_transitions", {"key": key}, purpose="available transitions")
        available = trans.output.get("transitions", []) if trans.ok else []
        path = []
        if target_status in available:
            path = [target_status]
        elif target_status == "In Review" and "In Progress" in available:
            path = ["In Progress", "In Review"]
        for status in path:
            t = self.call(inv, "jira_transition", {"key": key, "status": status}, purpose=f"transition ticket to {status}")
            if t.ok or t.dry_run:
                done["transition"] = status
            else:
                break
        return done
