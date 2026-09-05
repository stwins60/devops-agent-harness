"""Filesystem tools, sandboxed to the task workspace / project root."""
from __future__ import annotations

import difflib
import fnmatch
import re
from pathlib import Path
from typing import Any

from agent.audit.redaction import contains_secret, redact_text
from agent.models import PermissionLevel, RiskLevel, ToolResult
from tools.base import Tool, ToolContext, ToolError, tool

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".terraform", "dist", "build", ".mypy_cache", ".pytest_cache"}


def allowed_roots(ctx: ToolContext) -> list[Path]:
    roots = []
    for r in (ctx.workspace, ctx.project_root):
        if r:
            roots.append(Path(r).resolve())
    if not roots:
        roots.append(Path.cwd().resolve())
    return roots


def resolve_path(raw: str, ctx: ToolContext, *, must_exist: bool = False) -> Path:
    base = Path(ctx.workspace or ctx.project_root or Path.cwd())
    p = Path(raw)
    p = (p if p.is_absolute() else base / p).resolve()
    for root in allowed_roots(ctx):
        try:
            p.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise ToolError(f"path '{raw}' is outside the allowed workspace roots", kind="permission",
                        advice="only files under the task workspace or project root may be accessed")
    if must_exist and not p.exists():
        raise ToolError(f"path not found: {raw}", kind="not_found")
    return p


def _rel(p: Path, ctx: ToolContext) -> str:
    for root in allowed_roots(ctx):
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(p)


@tool("fs_read", "Read a text file from the workspace (secrets in the content are redacted).", category="filesystem",
      input_schema={"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["path"]},
      permissions=["filesystem.read"])
def fs_read(args: dict[str, Any], ctx: ToolContext) -> Any:
    p = resolve_path(args["path"], ctx, must_exist=True)
    if p.is_dir():
        raise ToolError(f"'{args['path']}' is a directory", kind="invalid")
    limit = int(args.get("max_chars") or 60000)
    text = p.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > limit
    return {"path": _rel(p, ctx), "content": redact_text(text[:limit]), "truncated": truncated, "lines": text.count("\n") + 1}


@tool("fs_list", "List files and directories under a path in the workspace.", category="filesystem",
      input_schema={"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}, "max_entries": {"type": "integer"}}},
      permissions=["filesystem.read"])
def fs_list(args: dict[str, Any], ctx: ToolContext) -> Any:
    p = resolve_path(args.get("path") or ".", ctx, must_exist=True)
    limit = int(args.get("max_entries") or 500)
    entries = []
    if args.get("recursive"):
        for child in p.rglob("*"):
            if any(part in _SKIP_DIRS for part in child.relative_to(p).parts):
                continue
            entries.append({"path": _rel(child, ctx), "type": "dir" if child.is_dir() else "file", "size": child.stat().st_size if child.is_file() else None})
            if len(entries) >= limit:
                break
    else:
        for child in sorted(p.iterdir()):
            entries.append({"path": _rel(child, ctx), "type": "dir" if child.is_dir() else "file", "size": child.stat().st_size if child.is_file() else None})
    return {"root": _rel(p, ctx), "entries": entries, "count": len(entries)}


@tool("fs_glob", "Find files matching a glob pattern (e.g. '**/*.yaml').", category="filesystem",
      input_schema={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]},
      permissions=["filesystem.read"])
def fs_glob(args: dict[str, Any], ctx: ToolContext) -> Any:
    p = resolve_path(args.get("path") or ".", ctx, must_exist=True)
    matches = []
    for child in p.glob(args["pattern"]):
        if any(part in _SKIP_DIRS for part in child.relative_to(p).parts):
            continue
        if child.is_file():
            matches.append(_rel(child, ctx))
        if len(matches) >= 1000:
            break
    return {"pattern": args["pattern"], "matches": sorted(matches)}


@tool("fs_search", "Search file contents with a regular expression.", category="filesystem",
      input_schema={"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
                                                     "max_results": {"type": "integer"}}, "required": ["pattern"]},
      permissions=["filesystem.read"])
def fs_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    p = resolve_path(args.get("path") or ".", ctx, must_exist=True)
    rx = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
    file_glob = args.get("glob") or "*"
    limit = int(args.get("max_results") or 200)
    hits = []
    files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and not any(part in _SKIP_DIRS for part in f.relative_to(p).parts)]
    for f in files:
        if not fnmatch.fnmatch(f.name, file_glob):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append({"file": _rel(f, ctx), "line": n, "text": redact_text(line.strip()[:300])})
                if len(hits) >= limit:
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


class FsWriteTool(Tool):
    """Write a file; keeps the previous content so the change can be rolled back."""

    def __init__(self) -> None:
        from agent.models import ToolSpec

        super().__init__(ToolSpec(
            name="fs_write", description="Create or overwrite a text file in the workspace. Refuses content containing secrets.",
            permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW, category="filesystem", mutating=True,
            permissions=["filesystem.write"], rollback="restore previous content of {path}",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        ))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx)
        content = str(args["content"])
        if contains_secret(content):
            raise ToolError("refusing to write content that appears to contain a secret", kind="invalid",
                            advice="reference secrets via environment variables or a secret manager instead")
        previous = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        if ctx.dry_run:
            diff = unified_diff(previous or "", content, _rel(p, ctx))
            return ToolResult(ok=True, output={"path": _rel(p, ctx), "dry_run": True, "diff": diff}, tool=self.name, args=args, dry_run=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        diff = unified_diff(previous or "", content, _rel(p, ctx))
        return ToolResult(ok=True, output={"path": _rel(p, ctx), "bytes": len(content.encode('utf-8')), "diff": diff, "previous": previous,
                                           "created": previous is None}, tool=self.name, args=args)

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx)
        previous = (result.output or {}).get("previous") if isinstance(result.output, dict) else None
        if ctx.mock and ctx.backends.get("world") is not None and ctx.backends["world"].flags.get("rollback_fails"):
            return ToolResult(ok=False, error="simulated rollback failure (mock flag rollback_fails)", tool=f"{self.name}.rollback", args=args)
        if previous is None:
            if p.exists():
                p.unlink()
            return ToolResult(ok=True, output={"path": _rel(p, ctx), "action": "deleted (file did not exist before)"}, tool=f"{self.name}.rollback", args=args)
        p.write_text(previous, encoding="utf-8")
        return ToolResult(ok=True, output={"path": _rel(p, ctx), "action": "restored previous content"}, tool=f"{self.name}.rollback", args=args)


class FsReplaceTool(Tool):
    """Targeted edit: replace an exact substring (must be unique unless replace_all)."""

    def __init__(self) -> None:
        from agent.models import ToolSpec

        super().__init__(ToolSpec(
            name="fs_replace", description="Replace an exact text snippet inside a file (unique match required unless replace_all=true).",
            permission=PermissionLevel.MODIFY, risk_level=RiskLevel.LOW, category="filesystem", mutating=True,
            permissions=["filesystem.write"], rollback="restore previous content of {path}",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                                                           "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]},
        ))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx, must_exist=True)
        old, new = str(args["old"]), str(args["new"])
        if contains_secret(new):
            raise ToolError("refusing to write content that appears to contain a secret", kind="invalid")
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"snippet not found in {args['path']}", kind="not_found")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"snippet occurs {count} times in {args['path']}; pass replace_all=true or make it unique", kind="invalid")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        diff = unified_diff(text, updated, _rel(p, ctx))
        if ctx.dry_run:
            return ToolResult(ok=True, output={"path": _rel(p, ctx), "dry_run": True, "diff": diff, "replacements": count}, tool=self.name, args=args, dry_run=True)
        p.write_text(updated, encoding="utf-8")
        return ToolResult(ok=True, output={"path": _rel(p, ctx), "diff": diff, "replacements": count, "previous": text}, tool=self.name, args=args)

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx)
        previous = (result.output or {}).get("previous") if isinstance(result.output, dict) else None
        if ctx.mock and ctx.backends.get("world") is not None and ctx.backends["world"].flags.get("rollback_fails"):
            return ToolResult(ok=False, error="simulated rollback failure (mock flag rollback_fails)", tool=f"{self.name}.rollback", args=args)
        if previous is None:
            return ToolResult(ok=False, error="no previous content recorded", tool=f"{self.name}.rollback", args=args)
        p.write_text(previous, encoding="utf-8")
        return ToolResult(ok=True, output={"path": _rel(p, ctx), "action": "restored previous content"}, tool=f"{self.name}.rollback", args=args)


def unified_diff(before: str, after: str, name: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{name}", tofile=f"b/{name}"))


def build_tools() -> list[Tool]:
    return [fs_read, fs_list, fs_glob, fs_search, FsWriteTool(), FsReplaceTool()]
