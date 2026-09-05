"""Shared fixtures: every test runs against an isolated project root and a fresh MockWorld."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import HarnessConfig  # noqa: E402
from agent.harness import Harness  # noqa: E402
from tools.mock.world import MockWorld  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith("DEVOPS_AGENT_") or var in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PAT", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_URL",
                                                       "GITLAB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_PROFILE", "AWS_REGION"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def make_config(project_root: Path):
    def _make(**overrides):
        base = {"mock": True, "non_interactive": True, "auto_approve": True}
        base.update(overrides)
        return HarnessConfig.load(project_root, base)

    return _make


@pytest.fixture
def make_harness(make_config):
    def _make(scenario: str = "probe-port-mismatch", flags: dict | None = None, handler=None, allow_explicit: bool = True, provider=None, **overrides):
        cfg = make_config(mock_scenario=scenario, **overrides)
        cfg.extra["approve_all"] = allow_explicit
        world = MockWorld.build(scenario, flags=flags)
        return Harness(cfg, world=world, approval_handler=handler, provider=provider)

    return _make


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    import shutil

    src = ROOT / "examples" / "sample-app"
    dst = tmp_path / "sample-app"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst
