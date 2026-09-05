"""Persistent project memory under ``.agent/``.

Categories map to directories: memory/ decisions/ runbooks/ architecture/
incidents/ conventions/. Entries are markdown files with a small YAML front
matter. Writes are refused when the content looks like it contains a secret.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.audit.redaction import contains_secret
from agent.models import now_iso

CATEGORIES = ("memory", "decisions", "runbooks", "architecture", "incidents", "conventions")


class MemoryError(Exception):
    pass


@dataclass
class MemoryEntry:
    category: str
    name: str
    title: str
    content: str
    path: Path
    updated: str
    tags: list[str]

    def summary(self, limit: int = 240) -> str:
        body = self.content.strip().replace("\n", " ")
        return body[:limit] + ("..." if len(body) > limit else "")


class MemoryStore:
    def __init__(self, agent_dir: Path) -> None:
        self.root = Path(agent_dir)

    def _dir(self, category: str) -> Path:
        if category not in CATEGORIES:
            raise MemoryError(f"unknown memory category '{category}' (expected one of {', '.join(CATEGORIES)})")
        d = self.root / category
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def slug(title: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return s[:80] or "entry"

    def remember(self, category: str, title: str, content: str, *, tags: Optional[list[str]] = None,
                 overwrite: bool = True) -> Path:
        if contains_secret(content) or contains_secret(title):
            raise MemoryError("refusing to store memory: content appears to contain a secret")
        d = self._dir(category)
        path = d / f"{self.slug(title)}.md"
        if path.exists() and not overwrite:
            raise MemoryError(f"memory entry already exists: {path}")
        front = ["---", f"title: {title}", f"updated: {now_iso()}", f"tags: [{', '.join(tags or [])}]", "---", ""]
        path.write_text("\n".join(front) + content.strip() + "\n", encoding="utf-8")
        return path

    def load(self, category: str, name: str) -> Optional[MemoryEntry]:
        path = self.root / category / (name if name.endswith(".md") else f"{name}.md")
        return self._parse(category, path) if path.exists() else None

    def list(self, category: Optional[str] = None) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for cat in ([category] if category else CATEGORIES):
            d = self.root / cat
            if not d.exists():
                continue
            for p in sorted(d.glob("*.md")):
                entry = self._parse(cat, p)
                if entry:
                    out.append(entry)
        return out

    def recall(self, query: str, *, category: Optional[str] = None, limit: int = 5) -> list[MemoryEntry]:
        """Keyword recall: score entries by overlapping terms in title/tags/content."""
        terms = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower())}
        if not terms:
            return []
        scored: list[tuple[int, MemoryEntry]] = []
        for e in self.list(category):
            hay_title = e.title.lower()
            hay = e.content.lower()
            score = sum(3 for t in terms if t in hay_title) + sum(2 for t in terms if t in " ".join(e.tags).lower())
            score += sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]

    def _parse(self, category: str, path: Path) -> Optional[MemoryEntry]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        title, updated, tags, body = path.stem, "", [], text
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if m:
            head, body = m.group(1), m.group(2)
            for line in head.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip()
                elif line.startswith("updated:"):
                    updated = line.split(":", 1)[1].strip()
                elif line.startswith("tags:"):
                    tags = [t.strip() for t in line.split(":", 1)[1].strip().strip("[]").split(",") if t.strip()]
        return MemoryEntry(category, path.stem, title, body, path, updated, tags)

    def context_summary(self, query: str, limit_chars: int = 1500) -> str:
        parts = []
        for e in self.recall(query, limit=6):
            parts.append(f"- [{e.category}] {e.title}: {e.summary(200)}")
        text = "\n".join(parts)
        return text[:limit_chars]
