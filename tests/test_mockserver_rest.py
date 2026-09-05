"""Exercise the REAL REST backends (urllib) against the mock Jira/GitHub HTTP server."""
import threading

import pytest

from apps.mockserver.server import make_server
from tools.base import ToolError
from tools.github.tools import GitHubRestBackend
from tools.http import HttpClient
from tools.jira.tools import JiraRestBackend
from tools.mock.world import MockWorld


@pytest.fixture
def server():
    world = MockWorld.build("ci-failure")
    srv = make_server("127.0.0.1", 0, world)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, world
    srv.shutdown()


def test_jira_rest_backend_roundtrip(server, monkeypatch):
    srv, world = server
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_API_VERSION", "3")
    be = JiraRestBackend(base)
    issue = be.get_issue("DEVOPS-382")
    assert issue["summary"].startswith("API pods failing") and issue["acceptance_criteria"] and issue["status"] == "To Do"
    assert be.transitions("DEVOPS-382") == ["In Progress"]
    assert be.transition("DEVOPS-382", "In Progress")["status"] == "In Progress"
    be.add_comment("DEVOPS-382", "hello from the agent")
    assert world.jira["issues"]["DEVOPS-382"]["comments"][-1]["body"] == "hello from the agent"
    be.add_labels("DEVOPS-382", ["devops-agent"])
    assert "devops-agent" in world.jira["issues"]["DEVOPS-382"]["labels"]
    with pytest.raises(ToolError) as exc:
        be.get_issue("DEVOPS-999")
    assert exc.value.kind == "not_found"
    with pytest.raises(ToolError) as exc2:
        be.transition("DEVOPS-382", "Done")
    assert exc2.value.kind == "invalid"


def test_jira_rest_backend_requires_credentials(monkeypatch):
    with pytest.raises(ToolError) as exc:
        JiraRestBackend("http://localhost:1")
    assert exc.value.kind == "auth"


def test_github_rest_backend_roundtrip(server, monkeypatch):
    srv, world = server
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    be = GitHubRestBackend(f"http://127.0.0.1:{srv.server_port}")
    runs = be.workflow_runs("example-org/sample-app")
    assert runs[0]["conclusion"] == "failure" and runs[0]["id"] == 994
    jobs = be.run_jobs("example-org/sample-app", 994)
    assert jobs[0]["name"] == "lint"
    assert "F401" in be.job_logs("example-org/sample-app", 5002)
    pr = be.create_pr("example-org/sample-app", "fix/DEVOPS-1-x", "main", "title", "body")
    assert pr["number"] == 421 and pr["html_url"].endswith("/pull/421")
    assert be.get_pr("example-org/sample-app", 421)["title"] == "title"
    assert be.add_pr_comment("example-org/sample-app", 421, "lgtm")["body"] == "lgtm"
    assert be.commits("example-org/sample-app")[0]["sha"] == "9f1c2ab"
    with pytest.raises(ToolError) as exc:
        be.get_pr("other/repo", 1)
    assert exc.value.kind == "not_found"


def test_http_client_classifies_errors_and_never_leaks_token(server, monkeypatch):
    srv, _ = server
    client = HttpClient(f"http://127.0.0.1:{srv.server_port}", token="secret-token")
    with pytest.raises(ToolError) as exc:
        client.get("/rest/api/3/issue/NOPE-1")
    assert exc.value.kind == "not_found" and "secret-token" not in str(exc.value)
    with pytest.raises(ToolError) as exc2:
        HttpClient("http://127.0.0.1:9", max_retries=0).get("/x")
    assert exc2.value.kind == "network"
