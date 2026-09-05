"""Git / repository / PR specialist: inspects repositories, reviews PRs and delivers changes as branch -> commit -> push -> PR."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from agent.audit.redaction import contains_secret
from agent.models import Diagnosis, Hypothesis, Plan, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class GitSpecialist(Specialist):
    name = "git-agent"
    description = "Inspects repositories, reviews pull requests and delivers changes via feature branches and PRs (never to protected branches)."
    domains = ["git"]
    keywords = ["git", "branch", "merge", "rebase", "pull request", "pr", "merge request", "commit", "push", "review", "repository", "repo"]

    def investigate(self, inv: Investigation) -> None:
        if inv.target("pr"):
            self._review_pr(inv, int(inv.target("pr")))
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        if not repo or not repo.exists():
            return
        st = self.call(inv, "git_status", {"repo": str(repo)}, purpose="working tree status")
        if st.ok:
            inv.log.fact(f"Repository on branch '{st.output.get('branch')}', clean={st.output.get('clean')}, {len(st.output.get('entries', []))} modified path(s).",
                         source="git_status", git_branch=st.output.get("branch"), git_clean=st.output.get("clean"))
        log = self.call(inv, "git_log", {"repo": str(repo), "n": 5}, purpose="recent commits")
        if log.ok and log.output.get("commits"):
            commits = log.output["commits"]
            inv.log.fact("Recent commits: " + "; ".join(f"{c.get('sha', '')[:7]} {c.get('message', '')[:60]}" for c in commits[:3]), source="git_log", recent_commits=commits[:5])
            inv.task.links.commit = inv.task.links.commit or commits[0].get("sha", "")[:7]
        ls = self.call(inv, "fs_list", {"path": ".", "recursive": True, "max_entries": 300}, purpose="repository layout")
        if ls.ok:
            paths = [e["path"] for e in ls.output.get("entries", []) if e["type"] == "file"]
            kinds = {
                "kubernetes manifests": [p for p in paths if re.search(r"(^|/)(k8s|kubernetes|manifests|deploy)/.*\.ya?ml$", p)],
                "helm charts": [p for p in paths if p.endswith("Chart.yaml")], "terraform": [p for p in paths if p.endswith(".tf")],
                "dockerfiles": [p for p in paths if p.split("/")[-1].startswith("Dockerfile")], "ci": [p for p in paths if ".github/workflows" in p or p.endswith(".gitlab-ci.yml") or p == "Jenkinsfile"],
                "tests": [p for p in paths if re.search(r"(^|/)tests?/", p)], "ansible": [p for p in paths if re.search(r"(playbook|site)\.ya?ml$|(^|/)roles/", p)],
            }
            summary = ", ".join(f"{k}: {len(v)}" for k, v in kinds.items() if v)
            inv.log.fact(f"Repository contains {len(paths)} files ({summary or 'no infrastructure artefacts detected'}).", source="fs_list", repo_files=len(paths),
                         repo_kinds={k: v[:10] for k, v in kinds.items() if v})
            for key, values in kinds.items():
                if values:
                    inv.set_target(key.replace(" ", "_"), values)

    def _review_pr(self, inv: Investigation, number: int) -> None:
        pr = self.call(inv, "github_get_pr", {"number": number}, purpose="fetch pull request")
        if not pr.ok:
            return
        inv.task.links.pull_request = pr.output.get("html_url") or f"#{number}"
        inv.log.fact(f"PR #{number} '{pr.output.get('title')}' {pr.output.get('head', {}).get('ref')} -> {pr.output.get('base', {}).get('ref')} ({pr.output.get('state')}).",
                     source=f"github_get_pr({number})", pr_number=number, pr_base=pr.output.get("base", {}).get("ref"))
        files = self.call(inv, "github_pr_files", {"number": number}, purpose="changed files")
        if files.ok:
            names = [f.get("filename") for f in files.output.get("files", [])]
            risky = [n for n in names if re.search(r"(iam|secret|rbac|clusterrole|networkpolic|\.env|credentials|production)", n or "", re.I)]
            inv.log.fact(f"PR changes {len(names)} file(s): {names[:10]}{'; sensitive paths: ' + str(risky) if risky else ''}.", source="github_pr_files", pr_files=names, pr_risky_files=risky)
        comments = self.call(inv, "github_pr_comments", {"number": number}, purpose="existing review comments")
        if comments.ok:
            inv.log.fact(f"PR has {len(comments.output.get('comments', []))} comment(s).", source="github_pr_comments")
        checks = self.call(inv, "cicd_list_runs", {"branch": pr.output.get("head", {}).get("ref")}, purpose="CI status for the PR branch")
        if checks.ok and checks.output.get("runs"):
            r = checks.output["runs"][0]
            inv.log.fact(f"Latest CI run for the PR branch: {r.get('status')}/{r.get('conclusion')} ({r.get('url')}).", source="cicd_list_runs", pr_ci=r.get("conclusion"))
            inv.task.links.pipeline = r.get("url") or str(r.get("id"))

    def analyzers(self):
        return [("git.pr_review", _pr_review)]

    # ------------------------------------------------------------------
    def deliver(self, inv: Investigation, plan: Plan, *, ticket: Optional[str], title: str, body: str) -> dict[str, Any]:
        """Branch, commit, push and open a PR for the applied changes. Returns link info; stops at the first failure."""
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        result: dict[str, Any] = {"branch": None, "commit": None, "pr": None, "error": None}
        if not repo:
            result["error"] = "no workspace repository"
            return result
        if contains_secret(body) or contains_secret(title):
            result["error"] = "PR text contains a secret; refusing"
            return result
        diff = self.call(inv, "git_diff", {"repo": str(repo)}, purpose="review the diff before committing")
        if diff.ok and diff.output.get("empty") and not inv.task.dry_run:
            result["error"] = "no changes to commit"
            return result
        if diff.ok:
            inv.task.context["diff"] = diff.output.get("diff", "")[:20000]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if len(slug) > 40:
            slug = slug[:40].rsplit("-", 1)[0] if "-" in slug[:40] else slug[:40]
        prefix = "fix" if inv.task.kind.value in ("jira", "fix", "incident") else "feature"
        branch = f"{prefix}/{ticket}-{slug}" if ticket else f"{prefix}/{inv.task.id.lower()}-{slug}"
        br = self.call(inv, "git_create_branch", {"name": branch, "repo": str(repo)}, purpose="create fix branch")
        if not br.ok and not br.dry_run:
            result["error"] = br.error
            return result
        result["branch"] = branch
        inv.task.links.branch = branch
        paths = sorted({c.target for c in plan.changes if c.kind == "file" and (c.applied or inv.task.dry_run)})
        add = self.call(inv, "git_add", {"paths": paths or ["."], "repo": str(repo)}, purpose="stage changes")
        if not add.ok and not add.dry_run:
            result["error"] = add.error
            return result
        message = f"{ticket + ': ' if ticket else ''}{title}\n\n{body}\n\nCo-Authored-By: devops-agent <devops-agent@example.com>"
        commit = self.call(inv, "git_commit", {"message": message, "repo": str(repo)}, purpose="commit changes")
        if not commit.ok and not commit.dry_run:
            result["error"] = commit.error
            return result
        sha = (commit.output or {}).get("sha", "") if isinstance(commit.output, dict) else ""
        result["commit"] = sha[:7] if sha else ("dry-run" if commit.dry_run else None)
        inv.task.links.commit = result["commit"]
        push = self.call(inv, "git_push", {"branch": branch, "repo": str(repo)}, purpose=f"push {branch} to origin",
                         expected_impact="new remote branch; no protected branch is touched", resources=[f"branch: {branch}"])
        if not push.ok and not push.dry_run:
            result["error"] = f"push failed: {push.error}"
            return result
        provider = self.h.config.git_provider
        if provider == "gitlab":
            pr = self.call(inv, "gitlab_create_mr", {"source": branch, "target": "main", "title": f"{ticket + ': ' if ticket else ''}{title}", "description": body}, purpose="open merge request")
            url = (pr.output or {}).get("url") if pr.ok else None
        else:
            pr = self.call(inv, "github_create_pr", {"head": branch, "base": "main", "title": f"{ticket + ': ' if ticket else ''}{title}", "body": body}, purpose="open pull request",
                           expected_impact="a pull request is opened for human review; nothing is merged")
            url = (pr.output or {}).get("url") if pr.ok else None
        if not pr.ok and not pr.dry_run:
            result["error"] = f"PR creation failed: {pr.error}"
            return result
        result["pr"] = url or ("dry-run" if pr.dry_run else None)
        inv.task.links.pull_request = result["pr"]
        return result

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        if not repo:
            return []
        diff = self.call(inv, "git_diff", {"repo": str(repo)}, purpose="capture the diff")
        if diff.ok:
            text = diff.output.get("diff", "")
            inv.task.context["diff"] = text[:20000]
            return [ValidationResult("git diff captured", True, f"{text.count(chr(10))} diff lines", skipped=not text.strip())]
        return [ValidationResult("git diff captured", False, diff.error or "")]


def _pr_review(log: EvidenceLog) -> list[Hypothesis]:
    if not log.has("pr_number"):
        return []
    risky = log.get("pr_risky_files") or []
    ci = log.get("pr_ci")
    notes = []
    if risky:
        notes.append(f"touches sensitive paths {risky}: request security review")
    if ci and ci != "success":
        notes.append(f"CI conclusion is {ci}: do not merge until green")
    if log.get("pr_base") in ("main", "master", "production") and not notes:
        notes.append("changes are scoped and CI is green; suitable for human approval")
    log.recommendation("PR review: " + "; ".join(notes))
    return [Hypothesis(statement="PR review completed: " + "; ".join(notes), validation="file list, CI status and comments inspected", status="confirmed", confidence=0.8)]
