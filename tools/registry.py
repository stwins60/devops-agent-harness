"""Tool registry.

Tools are registered by name with their ToolSpec. The registry can export
its manifest as YAML (matching the format in docs/tools.md) and can be
filtered by category, permission level or tag.
"""
from __future__ import annotations

import fnmatch
from typing import Any, Callable, Iterable, Iterator, Optional

import yaml

from agent.models import PermissionLevel, ToolSpec
from tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration ----------------------------------------------------
    def register(self, tool: Tool, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool
        return tool

    def register_all(self, tools: Iterable[Tool], replace: bool = False) -> None:
        for t in tools:
            self.register(t, replace=replace)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- lookup ----------------------------------------------------------
    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool '{name}'") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(sorted(self._tools.values(), key=lambda t: t.name))

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self]

    def find(self, pattern: str = "*", *, category: Optional[str] = None, max_permission: Optional[PermissionLevel] = None,
             tag: Optional[str] = None, predicate: Optional[Callable[[ToolSpec], bool]] = None) -> list[Tool]:
        out: list[Tool] = []
        for t in self:
            s = t.spec
            if not fnmatch.fnmatch(s.name, pattern):
                continue
            if category and s.category != category:
                continue
            if max_permission is not None and s.permission > max_permission:
                continue
            if tag and tag not in s.tags:
                continue
            if predicate and not predicate(s):
                continue
            out.append(t)
        return out

    def categories(self) -> list[str]:
        return sorted({t.spec.category for t in self})

    # -- export ----------------------------------------------------------
    def manifest(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.specs()]

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.manifest(), sort_keys=False)

    def model_tool_definitions(self, max_permission: Optional[PermissionLevel] = None) -> list[dict[str, Any]]:
        """Provider-neutral tool definitions (name/description/input_schema) for LLM function calling."""
        defs = []
        for t in self.find(max_permission=max_permission):
            schema = t.spec.input_schema or {"type": "object", "properties": {}}
            defs.append({"name": t.name, "description": t.spec.description, "input_schema": schema})
        return defs
