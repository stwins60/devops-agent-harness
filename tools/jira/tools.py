"""Jira tools: Jira Cloud/Server REST backend + mock backend."""
from __future__ import annotations

import os
import re
from typing import Any, Optional, Protocol

from agent.audit.redaction import contains_secret
from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.http import HttpClient
from tools.mock.world import MockWorld


class JiraBackend(Protocol):
    def get_issue(self, key: str) -> dict[str, Any]: ...
    def search(self, jql: str, limit: int = 20) -> list[dict[str, Any]]: ...
    def add_comment(self, key: str, body: str) -> dict[str, Any]: ...
    def transitions(self, key: str) -> list[str]: ...
    def transition(self, key: str, name: str) -> dict[str, Any]: ...
    def add_labels(self, key: str, labels: list[str]) -> dict[str, Any]: ...
    def assign(self, key: str, assignee: Optional[str]) -> dict[str, Any]: ...
    def create_subtask(self, parent: str, summary: str, description: str) -> dict[str, Any]: ...
    def add_worklog(self, key: str, seconds: int, comment: str) -> dict[str, Any]: ...
    def link_issues(self, inward: str, outward: str, link_type: str) -> dict[str, Any]: ...
    def update_fields(self, key: str, fields: dict[str, Any]) -> dict[str, Any]: ...


def _adf(text: str) -> dict[str, Any]:
    """Atlassian Document Format for Jira Cloud v3 comments."""
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line] or
            [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        inner = "".join(_adf_to_text(c) for c in node.get("content", []))
        return inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem") else "")
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    return ""


class JiraRestBackend:
    def __init__(self, base_url: Optional[str] = None) -> None:
        url = base_url or os.environ.get("JIRA_URL")
        if not url:
            raise ToolError("JIRA_URL is not configured", kind="auth")
        email, token, pat = os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_API_TOKEN"), os.environ.get("JIRA_PAT")
        if pat:
            self.client = HttpClient(url, token=pat)
        elif email and token:
            self.client = HttpClient(url, basic_user=email, basic_password=token)
        else:
            raise ToolError("Jira credentials missing: set JIRA_EMAIL + JIRA_API_TOKEN (cloud) or JIRA_PAT (server)", kind="auth")
        self.api = "/rest/api/3" if "atlassian.net" in url or os.environ.get("JIRA_API_VERSION") == "3" else "/rest/api/2"

    def _normalise(self, raw: dict[str, Any]) -> dict[str, Any]:
        f = raw.get("fields", {})
        desc = f.get("description")
        return {
            "key": raw.get("key"), "summary": f.get("summary"), "description": _adf_to_text(desc) if isinstance(desc, dict) else (desc or ""),
            "status": (f.get("status") or {}).get("name"), "priority": (f.get("priority") or {}).get("name"),
            "labels": f.get("labels", []), "components": [c.get("name") for c in f.get("components", [])],
            "assignee": (f.get("assignee") or {}).get("displayName") or (f.get("assignee") or {}).get("name"),
            "reporter": (f.get("reporter") or {}).get("displayName"), "issuetype": (f.get("issuetype") or {}).get("name"),
            "links": [{"type": l.get("type", {}).get("name"), "key": (l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key")} for l in f.get("issuelinks", [])],
            "epic": (f.get("parent") or {}).get("key"), "sprint": None,
            "attachments": [{"filename": a.get("filename"), "size": a.get("size")} for a in f.get("attachment", [])],
            "comments": [{"author": (c.get("author") or {}).get("displayName"), "body": _adf_to_text(c.get("body")) if isinstance(c.get("body"), dict) else c.get("body")}
                         for c in (f.get("comment") or {}).get("comments", [])],
            "worklogs": [{"author": (w.get("author") or {}).get("displayName"), "seconds": w.get("timeSpentSeconds")} for w in (f.get("worklog") or {}).get("worklogs", [])],
            "acceptance_criteria": _extract_ac(_adf_to_text(desc) if isinstance(desc, dict) else (desc or "")),
        }

    def get_issue(self, key):
        return self._normalise(self.client.get(f"{self.api}/issue/{key}", params={"expand": "renderedFields"}))

    def search(self, jql, limit=20):
        data = self.client.get(f"{self.api}/search", params={"jql": jql, "maxResults": limit})
        return [self._normalise(i) for i in data.get("issues", [])]

    def add_comment(self, key, body):
        payload = {"body": _adf(body)} if self.api.endswith("3") else {"body": body}
        return self.client.post(f"{self.api}/issue/{key}/comment", payload)

    def transitions(self, key):
        data = self.client.get(f"{self.api}/issue/{key}/transitions")
        return [t.get("name") for t in data.get("transitions", [])]

    def transition(self, key, name):
        data = self.client.get(f"{self.api}/issue/{key}/transitions")
        for t in data.get("transitions", []):
            if t.get("name", "").lower() == name.lower():
                self.client.post(f"{self.api}/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return {"key": key, "status": name}
        raise ToolError(f"transition '{name}' not available for {key} (available: {[t.get('name') for t in data.get('transitions', [])]})", kind="invalid")

    def add_labels(self, key, labels):
        self.client.put(f"{self.api}/issue/{key}", {"update": {"labels": [{"add": l} for l in labels]}})
        return {"key": key, "labels_added": labels}

    def assign(self, key, assignee):
        self.client.put(f"{self.api}/issue/{key}/assignee", {"accountId": assignee} if self.api.endswith("3") else {"name": assignee})
        return {"key": key, "assignee": assignee}

    def create_subtask(self, parent, summary, description):
        parent_issue = self.client.get(f"{self.api}/issue/{parent}", params={"fields": "project"})
        payload = {"fields": {"project": {"key": parent_issue["fields"]["project"]["key"]}, "parent": {"key": parent}, "summary": summary,
                              "issuetype": {"name": "Sub-task"}, "description": _adf(description) if self.api.endswith("3") else description}}
        return self.client.post(f"{self.api}/issue", payload)

    def add_worklog(self, key, seconds, comment):
        payload = {"timeSpentSeconds": seconds, "comment": _adf(comment) if self.api.endswith("3") else comment}
        return self.client.post(f"{self.api}/issue/{key}/worklog", payload)

    def link_issues(self, inward, outward, link_type):
        return self.client.post(f"{self.api}/issueLink", {"type": {"name": link_type}, "inwardIssue": {"key": inward}, "outwardIssue": {"key": outward}})

    def update_fields(self, key, fields):
        self.client.put(f"{self.api}/issue/{key}", {"fields": fields})
        return {"key": key, "updated": list(fields)}


def _extract_ac(description: str) -> list[str]:
    m = re.search(r"acceptance criteria:?\s*\n((?:\s*[-*]\s.*\n?)+)", description, re.I)
    if not m:
        return []
    return [re.sub(r"^\s*[-*]\s*", "", l).strip() for l in m.group(1).splitlines() if l.strip()]


class MockJiraBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def _check(self) -> None:
        if self.world.flags.get("jira_unavailable") or not self.world.jira.get("available", True):
            raise ToolError("network error GET https://jira.example.com/rest/api/3/issue: [Errno 111] Connection refused", kind="network")

    def _issue(self, key: str) -> dict[str, Any]:
        self._check()
        try:
            return self.world.jira["issues"][key]
        except KeyError as exc:
            raise ToolError(f"HTTP 404 GET /rest/api/3/issue/{key}: Issue does not exist or you do not have permission to see it.", kind="not_found") from exc

    def get_issue(self, key):
        return dict(self._issue(key))

    def search(self, jql, limit=20):
        self._check()
        low = jql.lower()
        out = []
        for issue in self.world.jira["issues"].values():
            if "status" in low and issue["status"].lower() not in low:
                continue
            out.append(dict(issue))
        return out[:limit]

    def add_comment(self, key, body):
        issue = self._issue(key)
        c = {"author": "devops-agent", "body": body}
        issue.setdefault("comments", []).append(c)
        self.world.record("jira_comment", key=key)
        return {"id": len(issue["comments"]), **c}

    def transitions(self, key):
        issue = self._issue(key)
        return list(self.world.jira["transitions"].get(issue["status"], []))

    def transition(self, key, name):
        issue = self._issue(key)
        allowed = self.world.jira["transitions"].get(issue["status"], [])
        if name not in allowed:
            raise ToolError(f"HTTP 400: transition '{name}' is not valid from status '{issue['status']}' (allowed: {allowed})", kind="invalid")
        previous = issue["status"]
        issue["status"] = name
        self.world.record("jira_transition", key=key, from_status=previous, to=name)
        return {"key": key, "status": name, "previous": previous}

    def add_labels(self, key, labels):
        issue = self._issue(key)
        for l in labels:
            if l not in issue["labels"]:
                issue["labels"].append(l)
        return {"key": key, "labels": issue["labels"]}

    def assign(self, key, assignee):
        issue = self._issue(key)
        issue["assignee"] = assignee
        return {"key": key, "assignee": assignee}

    def create_subtask(self, parent, summary, description):
        self._issue(parent)
        n = len(self.world.jira["issues"]) + 1
        key = f"{parent.split('-')[0]}-{400 + n}"
        self.world.jira["issues"][key] = {"key": key, "summary": summary, "description": description, "status": "To Do", "priority": "Medium",
                                          "labels": [], "components": [], "assignee": None, "reporter": "devops-agent", "issuetype": "Sub-task",
                                          "comments": [], "acceptance_criteria": [], "links": [], "epic": None, "sprint": None, "attachments": [], "worklogs": [],
                                          "parent": parent}
        return {"key": key}

    def add_worklog(self, key, seconds, comment):
        issue = self._issue(key)
        issue.setdefault("worklogs", []).append({"author": "devops-agent", "seconds": seconds, "comment": comment})
        return {"key": key, "seconds": seconds}

    def link_issues(self, inward, outward, link_type):
        self._issue(inward)
        self._issue(outward)
        self.world.jira["issues"][inward].setdefault("links", []).append({"type": link_type, "key": outward})
        return {"inward": inward, "outward": outward, "type": link_type}

    def update_fields(self, key, fields):
        issue = self._issue(key)
        issue.update(fields)
        return {"key": key, "updated": list(fields)}


# ----------------------------------------------------------------------
def _key(args: dict[str, Any]) -> str:
    key = str(args.get("key", "")).strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", key):
        raise ToolError(f"invalid Jira issue key '{key}'", kind="invalid")
    return key


@tool("jira_get_issue", "Fetch a Jira issue: summary, description, acceptance criteria, comments, status, priority, labels, links, epic, sprint.",
      category="jira", permissions=["jira.read"], input_schema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
def jira_get_issue(args, ctx):
    return ctx.backend("jira").get_issue(_key(args))


@tool("jira_search", "Search issues with JQL.", category="jira", permissions=["jira.read"],
      input_schema={"type": "object", "properties": {"jql": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["jql"]})
def jira_search(args, ctx):
    return {"issues": ctx.backend("jira").search(args["jql"], int(args.get("limit") or 20))}


@tool("jira_get_transitions", "List available workflow transitions for an issue.", category="jira", permissions=["jira.read"],
      input_schema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
def jira_get_transitions(args, ctx):
    return {"transitions": ctx.backend("jira").transitions(_key(args))}


def _write_tool(name: str, description: str, fn, required: list[str], props: dict[str, Any], rollback: str, risk: RiskLevel = RiskLevel.LOW,
                requires_approval: bool = False) -> Tool:
    class _T(Tool):
        spec = ToolSpec(name=name, description=description, permission=PermissionLevel.MODIFY, risk_level=risk, requires_approval=requires_approval,
                        permissions=["jira.write"], category="jira", mutating=True, rollback=rollback,
                        input_schema={"type": "object", "properties": props, "required": required})

        def run(self, args, ctx):
            if ctx.dry_run:
                return self.dry_run_result(args, ctx)
            for k in ("body", "comment", "description", "summary"):
                if k in args and contains_secret(str(args[k])):
                    raise ToolError("refusing to send text containing a secret to Jira", kind="invalid")
            return ToolResult(ok=True, output=fn(ctx.backend("jira"), args), tool=self.name, args=args)

    _T.__name__ = name
    return _T()


def build_tools() -> list[Tool]:
    return [
        jira_get_issue, jira_search, jira_get_transitions,
        _write_tool("jira_add_comment", "Add a comment to an issue.", lambda be, a: be.add_comment(_key(a), a["body"]), ["key", "body"],
                    {"key": {"type": "string"}, "body": {"type": "string"}}, "delete the comment"),
        _write_tool("jira_transition", "Move an issue to another status.", lambda be, a: be.transition(_key(a), a["status"]), ["key", "status"],
                    {"key": {"type": "string"}, "status": {"type": "string"}}, "transition back to the previous status"),
        _write_tool("jira_add_labels", "Add labels to an issue.", lambda be, a: be.add_labels(_key(a), list(a["labels"])), ["key", "labels"],
                    {"key": {"type": "string"}, "labels": {"type": "array"}}, "remove the labels"),
        _write_tool("jira_assign", "Assign an issue.", lambda be, a: be.assign(_key(a), a.get("assignee")), ["key"],
                    {"key": {"type": "string"}, "assignee": {"type": "string"}}, "restore the previous assignee"),
        _write_tool("jira_create_subtask", "Create a sub-task under an issue.", lambda be, a: be.create_subtask(_key(a), a["summary"], a.get("description") or ""),
                    ["key", "summary"], {"key": {"type": "string"}, "summary": {"type": "string"}, "description": {"type": "string"}}, "delete the sub-task",
                    requires_approval=True),
        _write_tool("jira_add_worklog", "Log work on an issue.", lambda be, a: be.add_worklog(_key(a), int(a["seconds"]), a.get("comment") or ""),
                    ["key", "seconds"], {"key": {"type": "string"}, "seconds": {"type": "integer"}, "comment": {"type": "string"}}, "delete the worklog"),
        _write_tool("jira_link_issues", "Link two issues.", lambda be, a: be.link_issues(_key(a), str(a["other"]).upper(), a.get("type") or "Relates"),
                    ["key", "other"], {"key": {"type": "string"}, "other": {"type": "string"}, "type": {"type": "string"}}, "delete the issue link"),
        _write_tool("jira_update_fields", "Update issue fields.", lambda be, a: be.update_fields(_key(a), dict(a["fields"])), ["key", "fields"],
                    {"key": {"type": "string"}, "fields": {"type": "object"}}, "restore previous field values", risk=RiskLevel.MEDIUM, requires_approval=True),
    ]
