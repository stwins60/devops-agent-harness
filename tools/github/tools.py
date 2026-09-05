"""GitHub tools: REST backend (urllib) + mock backend. Covers PRs, reviews, comments and Actions runs."""
from __future__ import annotations

import os
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.http import HttpClient
from tools.mock.world import MockWorld


class GitHubBackend(Protocol):
    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool = False) -> dict[str, Any]: ...
    def get_pr(self, repo: str, number: int) -> dict[str, Any]: ...
    def list_prs(self, repo: str, state: str = "open") -> list[dict[str, Any]]: ...
    def pr_files(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    def pr_comments(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    def add_pr_comment(self, repo: str, number: int, body: str) -> dict[str, Any]: ...
    def review_pr(self, repo: str, number: int, event: str, body: str) -> dict[str, Any]: ...
    def workflow_runs(self, repo: str, branch: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]: ...
    def run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]: ...
    def job_logs(self, repo: str, job_id: int) -> str: ...
    def rerun(self, repo: str, run_id: int, failed_only: bool = True) -> dict[str, Any]: ...
    def commits(self, repo: str, branch: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]: ...


class GitHubRestBackend:
    def __init__(self, base_url: Optional[str] = None) -> None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise ToolError("GITHUB_TOKEN (or GH_TOKEN) is not set", kind="auth", advice="export a fine-grained token with repo/actions scopes")
        self.client = HttpClient(base_url or os.environ.get("GITHUB_API_URL", "https://api.github.com"), token=token,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def create_pr(self, repo, head, base, title, body, draft=False):
        return self.client.post(f"/repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body, "draft": draft})

    def get_pr(self, repo, number):
        return self.client.get(f"/repos/{repo}/pulls/{number}")

    def list_prs(self, repo, state="open"):
        return self.client.get(f"/repos/{repo}/pulls", params={"state": state, "per_page": 30})

    def pr_files(self, repo, number):
        return self.client.get(f"/repos/{repo}/pulls/{number}/files")

    def pr_comments(self, repo, number):
        return self.client.get(f"/repos/{repo}/issues/{number}/comments")

    def add_pr_comment(self, repo, number, body):
        return self.client.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def review_pr(self, repo, number, event, body):
        return self.client.post(f"/repos/{repo}/pulls/{number}/reviews", {"event": event, "body": body})

    def workflow_runs(self, repo, branch=None, limit=10):
        data = self.client.get(f"/repos/{repo}/actions/runs", params={"branch": branch, "per_page": limit})
        return data.get("workflow_runs", []) if isinstance(data, dict) else []

    def run_jobs(self, repo, run_id):
        data = self.client.get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
        return data.get("jobs", []) if isinstance(data, dict) else []

    def job_logs(self, repo, job_id):
        return str(self.client.get(f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True))

    def rerun(self, repo, run_id, failed_only=True):
        path = f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs" if failed_only else f"/repos/{repo}/actions/runs/{run_id}/rerun"
        self.client.post(path, {})
        return {"run_id": run_id, "rerun": True}

    def commits(self, repo, branch=None, limit=10):
        rows = self.client.get(f"/repos/{repo}/commits", params={"sha": branch, "per_page": limit})
        return [{"sha": c.get("sha", "")[:7], "message": c.get("commit", {}).get("message", "").splitlines()[0],
                 "author": c.get("commit", {}).get("author", {}).get("name"), "date": c.get("commit", {}).get("author", {}).get("date")} for c in rows or []]


class MockGitHubBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def _repo(self, repo: str) -> None:
        if repo != self.world.github["repo"]:
            raise ToolError(f"HTTP 404 GET /repos/{repo}: Not Found", kind="not_found")

    def create_pr(self, repo, head, base, title, body, draft=False):
        self._repo(repo)
        if self.world.flags.get("pr_create_fails"):
            raise ToolError("HTTP 422 POST /repos/example-org/sample-app/pulls: Validation Failed: A pull request already exists for example-org:" + head, kind="invalid")
        if head not in self.world.github.get("branches", []):
            raise ToolError(f"HTTP 422 POST /repos/{repo}/pulls: Validation Failed: head branch '{head}' has not been pushed", kind="invalid")
        number = self.world.github["next_pr"]
        self.world.github["next_pr"] += 1
        pr = {"number": number, "title": title, "body": body, "head": {"ref": head}, "base": {"ref": base}, "state": "open", "draft": draft,
              "html_url": f"https://github.com/{repo}/pull/{number}", "mergeable": True}
        self.world.github["prs"].append(pr)
        self.world.record("github_create_pr", number=number, head=head)
        return pr

    def get_pr(self, repo, number):
        self._repo(repo)
        for pr in self.world.github["prs"]:
            if pr["number"] == number:
                return pr
        raise ToolError(f"HTTP 404: pull request #{number} not found", kind="not_found")

    def list_prs(self, repo, state="open"):
        self._repo(repo)
        return [p for p in self.world.github["prs"] if state == "all" or p["state"] == state]

    def pr_files(self, repo, number):
        self.get_pr(repo, number)
        return [{"filename": "k8s/deployment.yaml", "status": "modified", "additions": 2, "deletions": 2}]

    def pr_comments(self, repo, number):
        self.get_pr(repo, number)
        return list(self.world.github["comments"].get(number, []))

    def add_pr_comment(self, repo, number, body):
        self.get_pr(repo, number)
        c = {"id": len(self.world.github["comments"].get(number, [])) + 1, "body": body, "user": {"login": "devops-agent"}}
        self.world.github["comments"].setdefault(number, []).append(c)
        return c

    def review_pr(self, repo, number, event, body):
        self.get_pr(repo, number)
        self.world.record("github_review", number=number, event=event)
        return {"id": 1, "state": event, "body": body}

    def workflow_runs(self, repo, branch=None, limit=10):
        self._repo(repo)
        runs = [r for r in self.world.github["workflow_runs"] if not branch or r["head_branch"] == branch]
        return runs[:limit]

    def run_jobs(self, repo, run_id):
        self._repo(repo)
        if run_id not in self.world.github["jobs"]:
            raise ToolError(f"HTTP 404: run {run_id} not found", kind="not_found")
        return list(self.world.github["jobs"][run_id])

    def job_logs(self, repo, job_id):
        self._repo(repo)
        if job_id not in self.world.github["job_logs"]:
            raise ToolError(f"HTTP 404: job {job_id} not found", kind="not_found")
        return self.world.github["job_logs"][job_id]

    def rerun(self, repo, run_id, failed_only=True):
        self._repo(repo)
        self.world.record("github_rerun", run_id=run_id)
        return {"run_id": run_id, "rerun": True}

    def commits(self, repo, branch=None, limit=10):
        self._repo(repo)
        return list(self.world.github["commits"])[:limit]


def _gh_repo(args: dict[str, Any], ctx: ToolContext) -> str:
    repo = args.get("repo") or (getattr(ctx.config, "github_repo", None) if ctx.config else None)
    if not repo:
        raise ToolError("no GitHub repository specified (args.repo or config github_repo)", kind="invalid")
    return str(repo)


@tool("github_get_pr", "Get a pull request.", category="github", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"number": {"type": "integer"}, "repo": {"type": "string"}}, "required": ["number"]})
def github_get_pr(args, ctx):
    return ctx.backend("github").get_pr(_gh_repo(args, ctx), int(args["number"]))


@tool("github_list_prs", "List pull requests.", category="github", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"state": {"type": "string"}, "repo": {"type": "string"}}})
def github_list_prs(args, ctx):
    return {"pull_requests": ctx.backend("github").list_prs(_gh_repo(args, ctx), args.get("state") or "open")}


@tool("github_pr_files", "List files changed by a pull request.", category="github", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"number": {"type": "integer"}, "repo": {"type": "string"}}, "required": ["number"]})
def github_pr_files(args, ctx):
    return {"files": ctx.backend("github").pr_files(_gh_repo(args, ctx), int(args["number"]))}


@tool("github_pr_comments", "List comments on a pull request.", category="github", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"number": {"type": "integer"}, "repo": {"type": "string"}}, "required": ["number"]})
def github_pr_comments(args, ctx):
    return {"comments": ctx.backend("github").pr_comments(_gh_repo(args, ctx), int(args["number"]))}


@tool("github_workflow_runs", "List GitHub Actions workflow runs (optionally for a branch).", category="cicd", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"branch": {"type": "string"}, "repo": {"type": "string"}, "limit": {"type": "integer"}}})
def github_workflow_runs(args, ctx):
    return {"runs": ctx.backend("github").workflow_runs(_gh_repo(args, ctx), args.get("branch"), int(args.get("limit") or 10))}


@tool("github_run_jobs", "List jobs (with step conclusions) of a workflow run.", category="cicd", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"run_id": {"type": "integer"}, "repo": {"type": "string"}}, "required": ["run_id"]})
def github_run_jobs(args, ctx):
    return {"jobs": ctx.backend("github").run_jobs(_gh_repo(args, ctx), int(args["run_id"]))}


@tool("github_job_logs", "Fetch logs of a workflow job.", category="cicd", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"job_id": {"type": "integer"}, "repo": {"type": "string"}}, "required": ["job_id"]})
def github_job_logs(args, ctx):
    text = ctx.backend("github").job_logs(_gh_repo(args, ctx), int(args["job_id"]))
    return {"text": text[-20000:], "lines": text.splitlines()[-400:]}


@tool("github_commits", "List recent commits on a branch.", category="github", permissions=["github.read"],
      input_schema={"type": "object", "properties": {"branch": {"type": "string"}, "repo": {"type": "string"}, "limit": {"type": "integer"}}})
def github_commits(args, ctx):
    return {"commits": ctx.backend("github").commits(_gh_repo(args, ctx), args.get("branch"), int(args.get("limit") or 10))}


class GitHubCreatePrTool(Tool):
    spec = ToolSpec(name="github_create_pr", description="Open a pull request from a pushed branch.", permission=PermissionLevel.MODIFY,
                    risk_level=RiskLevel.LOW, requires_approval=True, permissions=["github.write"], category="github", mutating=True,
                    rollback="close the pull request",
                    input_schema={"type": "object", "properties": {"head": {"type": "string"}, "base": {"type": "string"}, "title": {"type": "string"},
                                                                   "body": {"type": "string"}, "repo": {"type": "string"}, "draft": {"type": "boolean"}},
                                  "required": ["head", "title"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        pr = ctx.backend("github").create_pr(_gh_repo(args, ctx), args["head"], args.get("base") or "main", args["title"], args.get("body") or "",
                                              bool(args.get("draft")))
        return ToolResult(ok=True, output={"number": pr.get("number"), "url": pr.get("html_url"), "head": args["head"]}, tool=self.name, args=args)


class GitHubCommentTool(Tool):
    spec = ToolSpec(name="github_pr_comment", description="Add a comment to a pull request.", permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW,
                    permissions=["github.write"], category="github", mutating=True, rollback="delete the comment",
                    input_schema={"type": "object", "properties": {"number": {"type": "integer"}, "body": {"type": "string"}, "repo": {"type": "string"}}, "required": ["number", "body"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("github").add_pr_comment(_gh_repo(args, ctx), int(args["number"]), args["body"]), tool=self.name, args=args)


class GitHubReviewTool(Tool):
    spec = ToolSpec(name="github_pr_review", description="Submit a PR review (COMMENT, APPROVE or REQUEST_CHANGES).", permission=PermissionLevel.MODIFY,
                    risk_level=RiskLevel.MEDIUM, requires_approval=True, permissions=["github.write"], category="github", mutating=True,
                    rollback="dismiss the review",
                    input_schema={"type": "object", "properties": {"number": {"type": "integer"}, "event": {"type": "string"}, "body": {"type": "string"}, "repo": {"type": "string"}},
                                  "required": ["number", "event"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("github").review_pr(_gh_repo(args, ctx), int(args["number"]), args["event"], args.get("body") or ""), tool=self.name, args=args)


class GitHubRerunTool(Tool):
    spec = ToolSpec(name="github_rerun_workflow", description="Re-run failed jobs of a workflow run.", permission=PermissionLevel.DEPLOY, risk_level=RiskLevel.MEDIUM,
                    requires_approval=True, permissions=["github.actions"], category="cicd", mutating=True, rollback="cancel the new run",
                    input_schema={"type": "object", "properties": {"run_id": {"type": "integer"}, "repo": {"type": "string"}, "failed_only": {"type": "boolean"}}, "required": ["run_id"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("github").rerun(_gh_repo(args, ctx), int(args["run_id"]), bool(args.get("failed_only", True))), tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [github_get_pr, github_list_prs, github_pr_files, github_pr_comments, github_workflow_runs, github_run_jobs, github_job_logs, github_commits,
            GitHubCreatePrTool(), GitHubCommentTool(), GitHubReviewTool(), GitHubRerunTool()]
