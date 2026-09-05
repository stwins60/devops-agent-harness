"""Git tools: real git CLI backend and a mock backend that tracks branches/commits in memory
while computing real diffs against a file snapshot."""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command

_SKIP = {".git", ".mockgit", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


class GitBackend(Protocol):
    def status(self, repo: Path) -> dict[str, Any]: ...
    def diff(self, repo: Path, staged: bool = False, path: Optional[str] = None) -> str: ...
    def log(self, repo: Path, n: int = 10, path: Optional[str] = None) -> list[dict[str, Any]]: ...
    def current_branch(self, repo: Path) -> str: ...
    def branches(self, repo: Path) -> list[str]: ...
    def create_branch(self, repo: Path, name: str, from_ref: Optional[str] = None) -> str: ...
    def checkout(self, repo: Path, ref: str) -> str: ...
    def add(self, repo: Path, paths: list[str]) -> str: ...
    def commit(self, repo: Path, message: str) -> dict[str, Any]: ...
    def push(self, repo: Path, remote: str, branch: str, set_upstream: bool = True) -> str: ...
    def remote_url(self, repo: Path, remote: str = "origin") -> Optional[str]: ...
    def fetch(self, repo: Path, remote: str = "origin") -> str: ...
    def delete_branch(self, repo: Path, name: str) -> str: ...
    def init(self, repo: Path) -> str: ...


class GitCliBackend:
    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def _run(self, repo: Path, *args: str, timeout: Optional[int] = None, ok_codes: tuple[int, ...] = (0,)) -> str:
        out = run_command(["git", *args], cwd=repo, timeout=timeout or self.timeout, env_passthrough=("GIT_SSH_COMMAND", "SSH_AUTH_SOCK", "GIT_ASKPASS"))
        if out.returncode not in ok_codes or out.timed_out:
            msg = (out.stderr or out.stdout).strip()
            low = msg.lower()
            kind = "auth" if "authentication failed" in low or "could not read username" in low or "permission denied (publickey)" in low else \
                   "permission" if "protected branch" in low or "rejected" in low else "network" if "could not resolve host" in low or "connection" in low else \
                   "not_found" if "not a git repository" in low or "did not match any" in low else "timeout" if out.timed_out else "unknown"
            raise ToolError(f"git {args[0]} failed: {msg[:600]}", kind=kind)
        return out.stdout

    def status(self, repo: Path) -> dict[str, Any]:
        out = self._run(repo, "status", "--porcelain=v1", "-b")
        lines = out.splitlines()
        branch = lines[0][3:].split("...")[0] if lines and lines[0].startswith("##") else ""
        entries = [{"status": l[:2].strip(), "path": l[3:]} for l in lines[1:]]
        return {"branch": branch, "clean": not entries, "entries": entries}

    def diff(self, repo: Path, staged: bool = False, path: Optional[str] = None) -> str:
        args = ["diff"] + (["--cached"] if staged else []) + (["--", path] if path else [])
        return self._run(repo, *args)

    def log(self, repo: Path, n: int = 10, path: Optional[str] = None) -> list[dict[str, Any]]:
        out = self._run(repo, "log", f"-n{n}", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s", "--date=iso", *(["--", path] if path else []), ok_codes=(0, 128))
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append({"sha": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return rows

    def current_branch(self, repo: Path) -> str:
        return self._run(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def branches(self, repo: Path) -> list[str]:
        return [b.strip().lstrip("* ").strip() for b in self._run(repo, "branch", "--list").splitlines() if b.strip()]

    def create_branch(self, repo: Path, name: str, from_ref: Optional[str] = None) -> str:
        return self._run(repo, "checkout", "-b", name, *([from_ref] if from_ref else []))

    def checkout(self, repo: Path, ref: str) -> str:
        return self._run(repo, "checkout", ref)

    def add(self, repo: Path, paths: list[str]) -> str:
        return self._run(repo, "add", "--", *paths)

    def commit(self, repo: Path, message: str) -> dict[str, Any]:
        self._run(repo, "commit", "-m", message)
        sha = self._run(repo, "rev-parse", "HEAD").strip()
        return {"sha": sha, "message": message}

    def push(self, repo: Path, remote: str, branch: str, set_upstream: bool = True) -> str:
        return self._run(repo, "push", *(["-u"] if set_upstream else []), remote, branch, timeout=300)

    def remote_url(self, repo: Path, remote: str = "origin") -> Optional[str]:
        try:
            return self._run(repo, "remote", "get-url", remote).strip() or None
        except ToolError:
            return None

    def fetch(self, repo: Path, remote: str = "origin") -> str:
        return self._run(repo, "fetch", remote, timeout=300)

    def delete_branch(self, repo: Path, name: str) -> str:
        return self._run(repo, "branch", "-D", name)

    def init(self, repo: Path) -> str:
        out = self._run(repo, "init", "-q")
        self._run(repo, "config", "user.email", "devops-agent@example.com")
        self._run(repo, "config", "user.name", "devops-agent")
        return out


class MockGitBackend:
    """In-memory git: real unified diffs against a snapshot, fake shas, remote push simulated via MockWorld flags."""

    def __init__(self, world: MockWorld) -> None:
        self.world = world
        self.repos: dict[str, dict[str, Any]] = {}

    STATE_FILE = ".mockgit/state.json"

    def _state(self, repo: Path) -> dict[str, Any]:
        key = str(Path(repo).resolve())
        if key not in self.repos:
            persisted = Path(repo) / self.STATE_FILE
            if persisted.exists():  # like a real .git directory, mock state survives process restarts (task resume)
                import json

                data = json.loads(persisted.read_text(encoding="utf-8"))
                data["staged"] = set(data.get("staged", []))
                self.repos[key] = data
            else:
                self.init(repo)
        return self.repos[key]

    def _persist(self, repo: Path) -> None:
        import json

        st = self.repos[str(Path(repo).resolve())]
        target = Path(repo) / self.STATE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        data = dict(st)
        data["staged"] = sorted(st["staged"])
        target.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _snapshot(repo: Path) -> dict[str, str]:
        snap: dict[str, str] = {}
        for p in Path(repo).rglob("*"):
            if p.is_file() and not any(part in _SKIP for part in p.relative_to(repo).parts):
                try:
                    snap[p.relative_to(repo).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return snap

    def init(self, repo: Path) -> str:
        key = str(Path(repo).resolve())
        self.repos[key] = {"branch": "main", "branches": ["main"], "snapshot": self._snapshot(Path(repo)), "staged": set(),
                           "commits": [{"sha": "9f1c2ab0000000000000000000000000000000000", "author": "dev1", "date": "2026-09-03T10:00:00Z",
                                        "message": "release 1.4.2: listen on 8080 (DEVOPS-380)"}], "remote": "git@github.com:example-org/sample-app.git",
                           "pushed": []}
        self._persist(repo)
        return f"Initialized empty mock Git repository in {repo}"

    def _changes(self, repo: Path) -> dict[str, tuple[Optional[str], Optional[str]]]:
        st = self._state(repo)
        now = self._snapshot(Path(repo))
        changed: dict[str, tuple[Optional[str], Optional[str]]] = {}
        for path in set(now) | set(st["snapshot"]):
            before, after = st["snapshot"].get(path), now.get(path)
            if before != after:
                changed[path] = (before, after)
        return changed

    def status(self, repo: Path) -> dict[str, Any]:
        st = self._state(repo)
        entries = []
        for path, (before, after) in sorted(self._changes(repo).items()):
            code = "A" if before is None else "D" if after is None else "M"
            entries.append({"status": code if path in st["staged"] else f"{code}?" if before is None else f" {code}", "path": path})
        return {"branch": st["branch"], "clean": not entries, "entries": entries}

    def diff(self, repo: Path, staged: bool = False, path: Optional[str] = None) -> str:
        st = self._state(repo)
        out = []
        for p, (before, after) in sorted(self._changes(repo).items()):
            if path and p != path:
                continue
            if staged and p not in st["staged"]:
                continue
            out.append("".join(difflib.unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
                                                    fromfile=f"a/{p}", tofile=f"b/{p}")))
        return "".join(out)

    def log(self, repo: Path, n: int = 10, path: Optional[str] = None) -> list[dict[str, Any]]:
        return list(reversed(self._state(repo)["commits"]))[:n]

    def current_branch(self, repo: Path) -> str:
        return self._state(repo)["branch"]

    def branches(self, repo: Path) -> list[str]:
        return list(self._state(repo)["branches"])

    def create_branch(self, repo: Path, name: str, from_ref: Optional[str] = None) -> str:
        st = self._state(repo)
        if name in st["branches"]:
            raise ToolError(f"fatal: a branch named '{name}' already exists", kind="invalid")
        st["branches"].append(name)
        st["branch"] = name
        self.world.record("git_branch", name=name)
        self._persist(repo)
        return f"Switched to a new branch '{name}'"

    def checkout(self, repo: Path, ref: str) -> str:
        st = self._state(repo)
        if ref not in st["branches"]:
            raise ToolError(f"error: pathspec '{ref}' did not match any file(s) known to git", kind="not_found")
        st["branch"] = ref
        self._persist(repo)
        return f"Switched to branch '{ref}'"

    def add(self, repo: Path, paths: list[str]) -> str:
        st = self._state(repo)
        changes = self._changes(repo)
        for p in paths:
            if p in (".", "-A", "--all"):
                st["staged"].update(changes.keys())
            else:
                norm = Path(p).as_posix()
                matched = [c for c in changes if c == norm or c.startswith(norm.rstrip("/") + "/")]
                if not matched:
                    raise ToolError(f"fatal: pathspec '{p}' did not match any files", kind="not_found")
                st["staged"].update(matched)
        self._persist(repo)
        return ""

    def commit(self, repo: Path, message: str) -> dict[str, Any]:
        st = self._state(repo)
        if not st["staged"]:
            raise ToolError("nothing to commit, working tree clean (stage changes with git_add first)", kind="invalid")
        now = self._snapshot(Path(repo))
        for p in st["staged"]:
            if p in now:
                st["snapshot"][p] = now[p]
            else:
                st["snapshot"].pop(p, None)
        sha = hashlib.sha1(f"{message}{len(st['commits'])}{sorted(st['staged'])}".encode()).hexdigest()
        st["commits"].append({"sha": sha, "author": "devops-agent", "date": "2026-09-04T11:00:00Z", "message": message, "files": sorted(st["staged"])})
        st["staged"] = set()
        self.world.record("git_commit", sha=sha[:7], message=message)
        self._persist(repo)
        return {"sha": sha, "message": message}

    def push(self, repo: Path, remote: str, branch: str, set_upstream: bool = True) -> str:
        st = self._state(repo)
        if self.world.flags.get("git_push_rejected"):
            raise ToolError(f"! [remote rejected] {branch} -> {branch} (protected branch hook declined)\nerror: failed to push some refs to '{st['remote']}'", kind="permission")
        if branch not in st["branches"]:
            raise ToolError(f"error: src refspec {branch} does not match any", kind="not_found")
        st["pushed"].append(branch)
        self.world.github.setdefault("branches", []).append(branch)
        self.world.record("git_push", branch=branch, remote=remote)
        self._persist(repo)
        return f"To {st['remote']}\n * [new branch]      {branch} -> {branch}\nbranch '{branch}' set up to track '{remote}/{branch}'."

    def remote_url(self, repo: Path, remote: str = "origin") -> Optional[str]:
        return self._state(repo)["remote"]

    def fetch(self, repo: Path, remote: str = "origin") -> str:
        return ""

    def delete_branch(self, repo: Path, name: str) -> str:
        st = self._state(repo)
        if name == st["branch"]:
            st["branch"] = "main"
        if name in st["branches"] and name != "main":
            st["branches"].remove(name)
        self._persist(repo)
        return f"Deleted branch {name}"


# ----------------------------------------------------------------------
def _repo(args: dict[str, Any], ctx: ToolContext) -> Path:
    raw = args.get("repo") or ctx.workspace or ctx.project_root
    if not raw:
        raise ToolError("no repository path available", kind="invalid")
    p = Path(raw)
    if not p.exists():
        raise ToolError(f"repository path does not exist: {p}", kind="not_found")
    return p


def validate_branch_name(name: str) -> None:
    if not re.match(r"^(feature|fix|chore|hotfix|docs|refactor|ci|test)/[A-Za-z0-9._\-]+$", name):
        raise ToolError(f"branch name '{name}' does not follow <type>/<TICKET>-<description> convention", kind="invalid",
                        advice="use feature/DEVOPS-123-short-description, fix/..., chore/...")


@tool("git_status", "Show working tree status.", category="git", permissions=["git.read"], input_schema={"type": "object", "properties": {"repo": {"type": "string"}}})
def git_status(args: dict[str, Any], ctx: ToolContext) -> Any:
    return ctx.backend("git").status(_repo(args, ctx))


@tool("git_diff", "Show unstaged (or staged) changes as a unified diff.", category="git", permissions=["git.read"],
      input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "staged": {"type": "boolean"}, "path": {"type": "string"}}})
def git_diff(args: dict[str, Any], ctx: ToolContext) -> Any:
    diff = ctx.backend("git").diff(_repo(args, ctx), bool(args.get("staged")), args.get("path"))
    return {"diff": diff, "empty": not diff.strip()}


@tool("git_log", "Show recent commits.", category="git", permissions=["git.read"],
      input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "n": {"type": "integer"}, "path": {"type": "string"}}})
def git_log(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"commits": ctx.backend("git").log(_repo(args, ctx), int(args.get("n") or 10), args.get("path"))}


@tool("git_current_branch", "Show the checked-out branch and remote URL.", category="git", permissions=["git.read"],
      input_schema={"type": "object", "properties": {"repo": {"type": "string"}}})
def git_current_branch(args: dict[str, Any], ctx: ToolContext) -> Any:
    be = ctx.backend("git")
    repo = _repo(args, ctx)
    return {"branch": be.current_branch(repo), "remote": be.remote_url(repo), "branches": be.branches(repo)}


@tool("git_fetch", "Fetch from a remote (no working tree change).", category="git", permissions=["git.read"],
      input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "remote": {"type": "string"}}})
def git_fetch(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"output": ctx.backend("git").fetch(_repo(args, ctx), args.get("remote") or "origin")}


class GitCreateBranchTool(Tool):
    spec = ToolSpec(name="git_create_branch", description="Create and check out a new branch following <type>/<TICKET>-<description>.",
                    permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW, permissions=["git.write"], category="git", mutating=True,
                    rollback="git checkout <previous branch> && git branch -D {name}",
                    input_schema={"type": "object", "properties": {"name": {"type": "string"}, "repo": {"type": "string"}, "from_ref": {"type": "string"}}, "required": ["name"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        validate_branch_name(args["name"])
        repo = _repo(args, ctx)
        be = ctx.backend("git")
        previous = be.current_branch(repo)
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = be.create_branch(repo, args["name"], args.get("from_ref"))
        return ToolResult(ok=True, output={"branch": args["name"], "previous": previous, "output": out}, tool=self.name, args=args)

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> ToolResult:
        repo = _repo(args, ctx)
        be = ctx.backend("git")
        prev = (result.output or {}).get("previous", "main") if isinstance(result.output, dict) else "main"
        be.checkout(repo, prev)
        be.delete_branch(repo, args["name"])
        return ToolResult(ok=True, output={"checked_out": prev, "deleted": args["name"]}, tool=f"{self.name}.rollback", args=args)


class GitAddTool(Tool):
    spec = ToolSpec(name="git_add", description="Stage files.", permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW, permissions=["git.write"],
                    category="git", mutating=True, rollback="git reset -- <paths>",
                    input_schema={"type": "object", "properties": {"paths": {"type": "array"}, "repo": {"type": "string"}}, "required": ["paths"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        ctx.backend("git").add(_repo(args, ctx), [str(p) for p in args["paths"]])
        return ToolResult(ok=True, output={"staged": args["paths"]}, tool=self.name, args=args)


class GitCommitTool(Tool):
    spec = ToolSpec(name="git_commit", description="Commit staged changes.", permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW,
                    permissions=["git.write"], category="git", mutating=True, rollback="git reset --soft HEAD~1 (local only, before push)",
                    input_schema={"type": "object", "properties": {"message": {"type": "string"}, "repo": {"type": "string"}}, "required": ["message"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = ctx.backend("git").commit(_repo(args, ctx), args["message"])
        return ToolResult(ok=True, output=out, tool=self.name, args=args)


class GitPushTool(Tool):
    spec = ToolSpec(name="git_push", description="Push a branch to a remote. Pushes to protected branches are refused by policy.",
                    permission=PermissionLevel.MODIFY, risk_level=RiskLevel.MEDIUM, requires_approval=True, permissions=["git.push"],
                    category="git", mutating=True, timeout=300, rollback="delete the remote branch (git push origin --delete {branch}) if no PR was merged",
                    input_schema={"type": "object", "properties": {"branch": {"type": "string"}, "remote": {"type": "string"}, "repo": {"type": "string"}}, "required": ["branch"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = ctx.backend("git").push(_repo(args, ctx), args.get("remote") or "origin", args["branch"])
        return ToolResult(ok=True, output={"branch": args["branch"], "output": out}, tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [git_status, git_diff, git_log, git_current_branch, git_fetch, GitCreateBranchTool(), GitAddTool(), GitCommitTool(), GitPushTool()]
