"""Mock Jira + GitHub HTTP API server backed by MockWorld.

Lets the *real* REST backends (JiraRestBackend, GitHubRestBackend) be exercised
without credentials: point JIRA_URL / GITHUB_API_URL at this server.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from tools.mock.world import MockWorld


def _jira_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {"key": issue["key"], "fields": {
        "summary": issue.get("summary"), "description": issue.get("description"), "status": {"name": issue.get("status")},
        "priority": {"name": issue.get("priority")}, "labels": issue.get("labels", []), "components": [{"name": c} for c in issue.get("components", [])],
        "assignee": {"displayName": issue.get("assignee")} if issue.get("assignee") else None, "reporter": {"displayName": issue.get("reporter")},
        "issuetype": {"name": issue.get("issuetype")}, "issuelinks": [{"type": {"name": l["type"]}, "outwardIssue": {"key": l["key"]}} for l in issue.get("links", [])],
        "comment": {"comments": [{"author": {"displayName": c["author"]}, "body": c["body"]} for c in issue.get("comments", [])]},
        "attachment": [], "worklog": {"worklogs": []},
    }}


class Handler(BaseHTTPRequestHandler):
    world: MockWorld
    lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: Any = None, raw: Optional[str] = None) -> None:
        payload = raw.encode() if raw is not None else json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def _route(self, method: str) -> None:
        path = self.path.split("?")[0]
        w = self.world
        with self.lock:
            if path == "/healthz":
                return self._send(200, {"ok": True})
            # ---- Jira ---------------------------------------------------
            m = re.match(r"^/rest/api/[23]/issue/([A-Z]+-\d+)(/comment|/transitions|/assignee|/worklog)?$", path)
            if m:
                key, sub = m.group(1), m.group(2)
                issue = w.jira["issues"].get(key)
                if not issue:
                    return self._send(404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]})
                if method == "GET" and not sub:
                    return self._send(200, _jira_issue(issue))
                if method == "GET" and sub == "/transitions":
                    return self._send(200, {"transitions": [{"id": str(i + 1), "name": n} for i, n in enumerate(w.jira["transitions"].get(issue["status"], []))]})
                if method == "POST" and sub == "/transitions":
                    tid = self._body().get("transition", {}).get("id")
                    names = w.jira["transitions"].get(issue["status"], [])
                    try:
                        issue["status"] = names[int(tid) - 1]
                    except (TypeError, ValueError, IndexError):
                        return self._send(400, {"errorMessages": ["invalid transition"]})
                    return self._send(204, raw="")
                if method == "POST" and sub == "/comment":
                    body = self._body().get("body")
                    text = body if isinstance(body, str) else " ".join(t.get("text", "") for p in body.get("content", []) for t in p.get("content", []))
                    issue.setdefault("comments", []).append({"author": "devops-agent", "body": text})
                    return self._send(201, {"id": str(len(issue["comments"])), "body": body})
                if method == "PUT" and not sub:
                    data = self._body()
                    for op in data.get("update", {}).get("labels", []):
                        if "add" in op and op["add"] not in issue["labels"]:
                            issue["labels"].append(op["add"])
                    issue.update(data.get("fields", {}))
                    return self._send(204, raw="")
                if method == "PUT" and sub == "/assignee":
                    issue["assignee"] = self._body().get("accountId") or self._body().get("name")
                    return self._send(204, raw="")
                if method == "POST" and sub == "/worklog":
                    issue.setdefault("worklogs", []).append(self._body())
                    return self._send(201, {"id": "1"})
            if path.startswith("/rest/api/") and path.endswith("/search"):
                return self._send(200, {"issues": [_jira_issue(i) for i in w.jira["issues"].values()]})
            if path.startswith("/rest/api/") and path.endswith("/issue") and method == "POST":
                data = self._body().get("fields", {})
                key = f"DEVOPS-{500 + len(w.jira['issues'])}"
                w.jira["issues"][key] = {"key": key, "summary": data.get("summary"), "description": "", "status": "To Do", "priority": "Medium", "labels": [],
                                         "components": [], "assignee": None, "reporter": "devops-agent", "issuetype": "Sub-task", "comments": [], "links": [],
                                         "acceptance_criteria": [], "epic": None, "sprint": None, "attachments": [], "worklogs": []}
                return self._send(201, {"key": key})
            # ---- GitHub -------------------------------------------------
            m = re.match(r"^/repos/([^/]+/[^/]+)/(.*)$", path)
            if m:
                repo, rest = m.group(1), m.group(2)
                if repo != w.github["repo"]:
                    return self._send(404, {"message": "Not Found"})
                if rest == "pulls" and method == "GET":
                    return self._send(200, w.github["prs"])
                if rest == "pulls" and method == "POST":
                    data = self._body()
                    if w.flags.get("pr_create_fails"):
                        return self._send(422, {"message": "Validation Failed"})
                    number = w.github["next_pr"]
                    w.github["next_pr"] += 1
                    pr = {"number": number, "title": data.get("title"), "body": data.get("body"), "head": {"ref": data.get("head")}, "base": {"ref": data.get("base")},
                          "state": "open", "html_url": f"https://github.com/{repo}/pull/{number}"}
                    w.github["prs"].append(pr)
                    return self._send(201, pr)
                pm = re.match(r"^pulls/(\d+)(/files|/reviews)?$", rest)
                if pm:
                    pr = next((p for p in w.github["prs"] if p["number"] == int(pm.group(1))), None)
                    if not pr:
                        return self._send(404, {"message": "Not Found"})
                    if pm.group(2) == "/files":
                        return self._send(200, [{"filename": "k8s/deployment.yaml", "status": "modified"}])
                    if pm.group(2) == "/reviews":
                        return self._send(200, {"id": 1, "state": self._body().get("event")})
                    return self._send(200, pr)
                im = re.match(r"^issues/(\d+)/comments$", rest)
                if im:
                    n = int(im.group(1))
                    if method == "POST":
                        c = {"id": 1, "body": self._body().get("body"), "user": {"login": "devops-agent"}}
                        w.github["comments"].setdefault(n, []).append(c)
                        return self._send(201, c)
                    return self._send(200, w.github["comments"].get(n, []))
                if rest == "actions/runs":
                    return self._send(200, {"workflow_runs": w.github["workflow_runs"]})
                rm = re.match(r"^actions/runs/(\d+)/jobs$", rest)
                if rm:
                    return self._send(200, {"jobs": w.github["jobs"].get(int(rm.group(1)), [])})
                rm = re.match(r"^actions/runs/(\d+)/rerun(-failed-jobs)?$", rest)
                if rm and method == "POST":
                    return self._send(201, {})
                jm = re.match(r"^actions/jobs/(\d+)/logs$", rest)
                if jm:
                    return self._send(200, raw=w.github["job_logs"].get(int(jm.group(1)), ""))
                if rest == "commits":
                    return self._send(200, [{"sha": c["sha"], "commit": {"message": c["message"], "author": {"name": c["author"], "date": c["date"]}}} for c in w.github["commits"]])
            return self._send(404, {"message": f"no mock route for {method} {path}"})


def make_server(host: str = "127.0.0.1", port: int = 8089, world: Optional[MockWorld] = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"world": world or MockWorld.build("probe-port-mismatch")})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    ap = argparse.ArgumentParser(description="mock Jira/GitHub API server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--scenario", default="probe-port-mismatch")
    a = ap.parse_args()
    server = make_server(a.host, a.port, MockWorld.build(a.scenario))
    print(f"mock Jira/GitHub API listening on http://{a.host}:{a.port} (scenario {a.scenario})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
