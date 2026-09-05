"""CI/CD tools: provider-neutral wrappers over GitHub Actions / GitLab CI backends, plus log analysis."""
from __future__ import annotations

import re
from typing import Any

from tools.base import Tool, ToolContext, ToolError, tool

_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (r"(ModuleNotFoundError|ImportError): No module named '([^']+)'", "missing python dependency", "add the module to requirements / install step"),
    (r"npm ERR! (.*)", "npm failure", "inspect package.json / lockfile"),
    (r"F401 '([^']+)' imported but unused", "lint: unused import", "remove the unused import"),
    (r"E501 line too long", "lint: line too long", "wrap the line or adjust the linter config"),
    (r"(\d+) failed, (\d+) passed", "test failures", "fix the failing tests"),
    (r"FAILED (\S+::\S+)", "failing test", "inspect the named test"),
    (r"Error: (.*not found.*)", "missing file/command", "check paths and installed tools in the runner image"),
    (r"permission denied", "permission denied", "check runner permissions / secrets"),
    (r"(ETIMEDOUT|timed out|Timeout)", "timeout", "increase timeout or fix the slow dependency"),
    (r"Error: Process completed with exit code (\d+)", "non-zero exit", "read the preceding lines for the actual error"),
    (r"##\[error\](.*)", "actions error annotation", "read the annotation"),
    (r"denied: requested access to the resource is denied", "registry auth failure", "check registry credentials"),
    (r"terraform.*Error: (.*)", "terraform error", "run terraform validate/plan locally"),
    (r"no space left on device", "runner disk full", "clean caches or use a larger runner"),
]


def analyze_log(text: str) -> dict[str, Any]:
    findings = []
    for pattern, label, hint in _ERROR_PATTERNS:
        for m in re.finditer(pattern, text, re.I):
            findings.append({"label": label, "match": m.group(0)[:200], "hint": hint})
    lines = text.splitlines()
    error_lines = [l for l in lines if re.search(r"error|fail|exception|traceback", l, re.I)][:20]
    return {"findings": findings[:20], "error_lines": error_lines, "line_count": len(lines)}


def _provider(args: dict[str, Any], ctx: ToolContext) -> str:
    p = args.get("provider") or (getattr(ctx.config, "git_provider", None) if ctx.config else None) or "github"
    if p not in ("github", "gitlab"):
        raise ToolError(f"unsupported CI provider '{p}'", kind="invalid")
    return p


@tool("cicd_list_runs", "List recent pipeline runs (GitHub Actions or GitLab CI) with status/conclusion.", category="cicd", permissions=["cicd.read"],
      input_schema={"type": "object", "properties": {"branch": {"type": "string"}, "provider": {"type": "string"}, "limit": {"type": "integer"}}})
def cicd_list_runs(args, ctx):
    p = _provider(args, ctx)
    limit = int(args.get("limit") or 10)
    if p == "github":
        repo = args.get("repo") or ctx.config.github_repo
        runs = ctx.backend("github").workflow_runs(repo, args.get("branch"), limit)
        return {"provider": p, "runs": [{"id": r["id"], "name": r.get("name"), "branch": r.get("head_branch"), "status": r.get("status"),
                                         "conclusion": r.get("conclusion"), "sha": r.get("head_sha"), "url": r.get("html_url"), "created": r.get("created_at")} for r in runs]}
    project = args.get("project") or ctx.config.gitlab_project
    runs = ctx.backend("gitlab").pipelines(project, args.get("branch"), limit)
    return {"provider": p, "runs": [{"id": r["id"], "branch": r.get("ref"), "status": r.get("status"), "conclusion": r.get("status"), "sha": r.get("sha"), "url": r.get("web_url")} for r in runs]}


@tool("cicd_run_jobs", "List jobs and step conclusions for a run/pipeline.", category="cicd", permissions=["cicd.read"],
      input_schema={"type": "object", "properties": {"run_id": {"type": "integer"}, "provider": {"type": "string"}}, "required": ["run_id"]})
def cicd_run_jobs(args, ctx):
    p = _provider(args, ctx)
    if p == "github":
        jobs = ctx.backend("github").run_jobs(args.get("repo") or ctx.config.github_repo, int(args["run_id"]))
        return {"provider": p, "jobs": [{"id": j["id"], "name": j.get("name"), "conclusion": j.get("conclusion"),
                                         "failed_steps": [s.get("name") for s in j.get("steps", []) if s.get("conclusion") == "failure"]} for j in jobs]}
    jobs = ctx.backend("gitlab").pipeline_jobs(args.get("project") or ctx.config.gitlab_project, int(args["run_id"]))
    return {"provider": p, "jobs": [{"id": j["id"], "name": j.get("name"), "conclusion": j.get("status"), "failed_steps": [j.get("name")] if j.get("status") == "failed" else []} for j in jobs]}


@tool("cicd_job_logs", "Fetch a job log and extract probable error causes.", category="cicd", permissions=["cicd.read"],
      input_schema={"type": "object", "properties": {"job_id": {"type": "integer"}, "provider": {"type": "string"}}, "required": ["job_id"]})
def cicd_job_logs(args, ctx):
    p = _provider(args, ctx)
    if p == "github":
        text = ctx.backend("github").job_logs(args.get("repo") or ctx.config.github_repo, int(args["job_id"]))
    else:
        text = ctx.backend("gitlab").job_log(args.get("project") or ctx.config.gitlab_project, int(args["job_id"]))
    return {"provider": p, "analysis": analyze_log(text), "tail": text[-6000:]}


def build_tools() -> list[Tool]:
    return [cicd_list_runs, cicd_run_jobs, cicd_job_logs]
