"""GitLab tools: REST backend + mock backend (merge requests and pipelines)."""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.http import HttpClient
from tools.mock.world import MockWorld


class GitLabBackend(Protocol):
    def create_mr(self, project: str, source: str, target: str, title: str, description: str) -> dict[str, Any]: ...
    def get_mr(self, project: str, iid: int) -> dict[str, Any]: ...
    def add_mr_note(self, project: str, iid: int, body: str) -> dict[str, Any]: ...
    def pipelines(self, project: str, ref: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]: ...
    def pipeline_jobs(self, project: str, pipeline_id: int) -> list[dict[str, Any]]: ...
    def job_log(self, project: str, job_id: int) -> str: ...
    def retry_pipeline(self, project: str, pipeline_id: int) -> dict[str, Any]: ...


class GitLabRestBackend:
    def __init__(self, base_url: Optional[str] = None) -> None:
        token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_PRIVATE_TOKEN")
        if not token:
            raise ToolError("GITLAB_TOKEN is not set", kind="auth")
        self.client = HttpClient((base_url or os.environ.get("GITLAB_URL", "https://gitlab.com")).rstrip("/") + "/api/v4", token=token,
                                 token_header="PRIVATE-TOKEN", token_prefix="")

    @staticmethod
    def _p(project: str) -> str:
        return urllib.parse.quote(project, safe="")

    def create_mr(self, project, source, target, title, description):
        return self.client.post(f"/projects/{self._p(project)}/merge_requests", {"source_branch": source, "target_branch": target, "title": title, "description": description})

    def get_mr(self, project, iid):
        return self.client.get(f"/projects/{self._p(project)}/merge_requests/{iid}")

    def add_mr_note(self, project, iid, body):
        return self.client.post(f"/projects/{self._p(project)}/merge_requests/{iid}/notes", {"body": body})

    def pipelines(self, project, ref=None, limit=10):
        return self.client.get(f"/projects/{self._p(project)}/pipelines", params={"ref": ref, "per_page": limit})

    def pipeline_jobs(self, project, pipeline_id):
        return self.client.get(f"/projects/{self._p(project)}/pipelines/{pipeline_id}/jobs")

    def job_log(self, project, job_id):
        return str(self.client.get(f"/projects/{self._p(project)}/jobs/{job_id}/trace", raw=True))

    def retry_pipeline(self, project, pipeline_id):
        return self.client.post(f"/projects/{self._p(project)}/pipelines/{pipeline_id}/retry", {})


class MockGitLabBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def create_mr(self, project, source, target, title, description):
        if self.world.flags.get("pr_create_fails"):
            raise ToolError("HTTP 409 POST merge_requests: Another open merge request already exists for this source branch", kind="invalid")
        iid = self.world.gitlab["next_mr"]
        self.world.gitlab["next_mr"] += 1
        mr = {"iid": iid, "title": title, "description": description, "source_branch": source, "target_branch": target, "state": "opened",
              "web_url": f"https://gitlab.example.com/{project}/-/merge_requests/{iid}"}
        self.world.gitlab["mrs"].append(mr)
        self.world.record("gitlab_create_mr", iid=iid)
        return mr

    def get_mr(self, project, iid):
        for mr in self.world.gitlab["mrs"]:
            if mr["iid"] == iid:
                return mr
        raise ToolError(f"HTTP 404: merge request !{iid} not found", kind="not_found")

    def add_mr_note(self, project, iid, body):
        self.get_mr(project, iid)
        return {"id": 1, "body": body}

    def pipelines(self, project, ref=None, limit=10):
        return [p for p in self.world.gitlab["pipelines"] if not ref or p["ref"] == ref][:limit]

    def pipeline_jobs(self, project, pipeline_id):
        if pipeline_id not in self.world.gitlab["jobs"]:
            raise ToolError(f"HTTP 404: pipeline {pipeline_id} not found", kind="not_found")
        return list(self.world.gitlab["jobs"][pipeline_id])

    def job_log(self, project, job_id):
        if job_id not in self.world.gitlab["job_logs"]:
            raise ToolError(f"HTTP 404: job {job_id} not found", kind="not_found")
        return self.world.gitlab["job_logs"][job_id]

    def retry_pipeline(self, project, pipeline_id):
        self.world.record("gitlab_retry", pipeline_id=pipeline_id)
        return {"id": pipeline_id + 1, "status": "pending"}


def _project(args: dict[str, Any], ctx: ToolContext) -> str:
    project = args.get("project") or (getattr(ctx.config, "gitlab_project", None) if ctx.config else None)
    if not project:
        raise ToolError("no GitLab project specified (args.project or config gitlab_project)", kind="invalid")
    return str(project)


@tool("gitlab_get_mr", "Get a merge request.", category="gitlab", permissions=["gitlab.read"],
      input_schema={"type": "object", "properties": {"iid": {"type": "integer"}, "project": {"type": "string"}}, "required": ["iid"]})
def gitlab_get_mr(args, ctx):
    return ctx.backend("gitlab").get_mr(_project(args, ctx), int(args["iid"]))


@tool("gitlab_pipelines", "List pipelines (optionally for a ref).", category="cicd", permissions=["gitlab.read"],
      input_schema={"type": "object", "properties": {"ref": {"type": "string"}, "project": {"type": "string"}, "limit": {"type": "integer"}}})
def gitlab_pipelines(args, ctx):
    return {"pipelines": ctx.backend("gitlab").pipelines(_project(args, ctx), args.get("ref"), int(args.get("limit") or 10))}


@tool("gitlab_pipeline_jobs", "List jobs of a pipeline.", category="cicd", permissions=["gitlab.read"],
      input_schema={"type": "object", "properties": {"pipeline_id": {"type": "integer"}, "project": {"type": "string"}}, "required": ["pipeline_id"]})
def gitlab_pipeline_jobs(args, ctx):
    return {"jobs": ctx.backend("gitlab").pipeline_jobs(_project(args, ctx), int(args["pipeline_id"]))}


@tool("gitlab_job_log", "Fetch the log (trace) of a job.", category="cicd", permissions=["gitlab.read"],
      input_schema={"type": "object", "properties": {"job_id": {"type": "integer"}, "project": {"type": "string"}}, "required": ["job_id"]})
def gitlab_job_log(args, ctx):
    text = ctx.backend("gitlab").job_log(_project(args, ctx), int(args["job_id"]))
    return {"text": text[-20000:], "lines": text.splitlines()[-400:]}


class GitLabCreateMrTool(Tool):
    spec = ToolSpec(name="gitlab_create_mr", description="Open a merge request.", permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW,
                    requires_approval=True, permissions=["gitlab.write"], category="gitlab", mutating=True, rollback="close the merge request",
                    input_schema={"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}, "title": {"type": "string"},
                                                                   "description": {"type": "string"}, "project": {"type": "string"}}, "required": ["source", "title"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        mr = ctx.backend("gitlab").create_mr(_project(args, ctx), args["source"], args.get("target") or "main", args["title"], args.get("description") or "")
        return ToolResult(ok=True, output={"iid": mr.get("iid"), "url": mr.get("web_url")}, tool=self.name, args=args)


class GitLabNoteTool(Tool):
    spec = ToolSpec(name="gitlab_mr_note", description="Add a note (comment) to a merge request.", permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW,
                    permissions=["gitlab.write"], category="gitlab", mutating=True, rollback="delete the note",
                    input_schema={"type": "object", "properties": {"iid": {"type": "integer"}, "body": {"type": "string"}, "project": {"type": "string"}}, "required": ["iid", "body"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("gitlab").add_mr_note(_project(args, ctx), int(args["iid"]), args["body"]), tool=self.name, args=args)


class GitLabRetryTool(Tool):
    spec = ToolSpec(name="gitlab_retry_pipeline", description="Retry a failed pipeline.", permission=PermissionLevel.DEPLOY, risk_level=RiskLevel.MEDIUM,
                    requires_approval=True, permissions=["gitlab.ci"], category="cicd", mutating=True, rollback="cancel the retried pipeline",
                    input_schema={"type": "object", "properties": {"pipeline_id": {"type": "integer"}, "project": {"type": "string"}}, "required": ["pipeline_id"]})

    def run(self, args, ctx):
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        return ToolResult(ok=True, output=ctx.backend("gitlab").retry_pipeline(_project(args, ctx), int(args["pipeline_id"])), tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [gitlab_get_mr, gitlab_pipelines, gitlab_pipeline_jobs, gitlab_job_log, GitLabCreateMrTool(), GitLabNoteTool(), GitLabRetryTool()]
