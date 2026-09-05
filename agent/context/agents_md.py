"""AGENTS.md discovery with hierarchical precedence.

Files are collected from the repository root down to the directory of the
target path. More specific files take precedence for *instructions*, but
security restrictions live in the policy engine and are never affected by
anything found in AGENTS.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

FILENAMES = ("AGENTS.md", "agents.md", "CLAUDE.md")


@dataclass
class InstructionFile:
    path: Path
    content: str
    depth: int  # 0 = repo root

    @property
    def sections(self) -> dict[str, str]:
        out: dict[str, str] = {}
        current = "_preamble"
        buf: list[str] = []
        for line in self.content.splitlines():
            m = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
            if m:
                out[current] = "\n".join(buf).strip()
                current = m.group(1).strip().lower()
                buf = []
            else:
                buf.append(line)
        out[current] = "\n".join(buf).strip()
        return {k: v for k, v in out.items() if v}


@dataclass
class InstructionSet:
    files: list[InstructionFile] = field(default_factory=list)

    def merged(self, max_chars: int = 6000) -> str:
        """Concatenate root -> specific so later (more specific) instructions override earlier ones."""
        parts = []
        for f in self.files:
            parts.append(f"<!-- {f.path} (precedence {f.depth}) -->\n{f.content.strip()}")
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 60] + "\n\n[... AGENTS.md content truncated to fit context budget ...]"
        return text

    def section(self, name: str) -> Optional[str]:
        """Most specific definition of a section wins."""
        name = name.lower()
        value = None
        for f in self.files:
            for k, v in f.sections.items():
                if name in k:
                    value = v
        return value

    def resolved_sections(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.files:  # root first, more specific later overrides
            for k, v in f.sections.items():
                out[k] = v
        return out

    @property
    def paths(self) -> list[str]:
        return [str(f.path) for f in self.files]


def find_repo_root(start: Path) -> Path:
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists() or any((candidate / n).exists() for n in FILENAMES):
            root = candidate
            # keep climbing while parents also have markers (monorepo) but stop at .git
            if (candidate / ".git").exists():
                return candidate
            # remember the highest AGENTS.md-bearing dir
            best = candidate
            for parent in candidate.parents:
                if (parent / ".git").exists():
                    return parent
                if any((parent / n).exists() for n in FILENAMES):
                    best = parent
            return best
    return p


def discover(target: Path, root: Optional[Path] = None) -> InstructionSet:
    target = Path(target).resolve()
    root = Path(root).resolve() if root else find_repo_root(target)
    directory = target if target.is_dir() else target.parent
    chain: list[Path] = []
    cur = directory
    while True:
        chain.append(cur)
        if cur == root or cur.parent == cur:
            break
        try:
            cur.relative_to(root)
        except ValueError:
            break
        cur = cur.parent
    chain.reverse()  # root first
    files: list[InstructionFile] = []
    for depth, d in enumerate(chain):
        for name in FILENAMES:
            p = d / name
            if p.exists():
                try:
                    files.append(InstructionFile(p, p.read_text(encoding="utf-8", errors="replace"), depth))
                except OSError:
                    continue
                break
    return InstructionSet(files)
