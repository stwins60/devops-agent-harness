import json
import os
import subprocess
import sys
from pathlib import Path

from agent.mcp.client import McpClient
from agent.mcp.server import HarnessMcpServer

ROOT = Path(__file__).resolve().parents[1]


def test_server_handles_protocol_in_process(make_harness):
    h = make_harness(mode="read-only")
    srv = HarnessMcpServer(h)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "devops-agent-harness"
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "kubectl_get" in names and "jira_get_issue" in names and all("inputSchema" in t for t in tools)
    call = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kubectl_get", "arguments": {"kind": "deployment", "name": "api", "namespace": "production"}}})
    payload = json.loads(call["result"]["content"][0]["text"])
    assert not call["result"]["isError"] and payload["status"]["readyReplicas"] == 0
    # policy still applies through MCP: mutation in read-only mode is refused and audited
    blocked = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kubectl_delete", "arguments": {"kind": "deployment", "name": "api"}}})
    assert blocked["result"]["isError"] and "policy" in json.loads(blocked["result"]["content"][0]["text"])["kind"]
    assert h.world.mutations == []
    unknown = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "nope", "params": {}})
    assert unknown["error"]["code"] == -32601


def test_client_talks_to_server_over_stdio(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    for k in list(env):
        if k.startswith("DEVOPS_AGENT_"):
            env.pop(k)
    cmd = [sys.executable, "-m", "apps.cli.main", "--mock", "--project-root", str(tmp_path), "mcp-serve"]
    client = McpClient(cmd, cwd=ROOT, name="harness", timeout=60)
    # McpClient sanitises the child environment; inject PYTHONPATH through its env map
    client.env = {"PYTHONPATH": str(ROOT)}
    with client:
        assert client.server_info.get("name") == "devops-agent-harness"
        tools = client.list_tools()
        assert any(t["name"] == "kubectl_events" for t in tools)
        out = client.call_tool("kubectl_events", {"namespace": "production"})
        assert out["count"] >= 1 and out["events"][0]["reason"] in ("Unhealthy", "Killing", "BackOff")
        try:
            client.call_tool("kubectl_delete", {"kind": "pod", "name": "x"})
        except Exception as exc:  # ToolError from isError
            assert "approval" in str(exc) or "policy" in str(exc) or "denied" in str(exc)
        else:
            raise AssertionError("mutation via MCP must not succeed without approval")


def test_preapproved_tools_pass_the_gate_over_mcp(make_harness):
    from agent.approvals.engine import build_handler

    h = make_harness(mode="approval", auto_approve=False, handler=build_handler(interactive=False, preapproved=["jira_add_comment"]))
    srv = HarnessMcpServer(h)
    ok = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "jira_add_comment", "arguments": {"key": "DEVOPS-382", "body": "hi"}}})
    assert not ok["result"]["isError"] and h.world.jira["issues"]["DEVOPS-382"]["comments"][-1]["body"] == "hi"
    denied = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "jira_transition", "arguments": {"key": "DEVOPS-382", "status": "In Progress"}}})
    assert denied["result"]["isError"] and json.loads(denied["result"]["content"][0]["text"])["kind"] == "denied"
    assert h.world.jira["issues"]["DEVOPS-382"]["status"] == "To Do"


def test_client_reports_missing_server_binary():
    client = McpClient(["definitely-not-a-real-binary-xyz"], name="x")
    try:
        client.start()
    except Exception as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected failure")


def test_tools_from_mcp_server_apply_risk_metadata(make_harness):
    from tools.adapters import tools_from_mcp_server

    class FakeClient:
        name = "fake"

        def list_tools(self):
            return [{"name": "get_issue", "description": "read", "inputSchema": {"type": "object"}},
                    {"name": "create_issue", "description": "write", "inputSchema": {"type": "object"}},
                    {"name": "delete_issue", "description": "destroy", "inputSchema": {"type": "object"}}]

    tools = tools_from_mcp_server(FakeClient(), {"name": "fake", "tools": {"delete_issue": {"disabled": True}, "create_issue": {"risk_level": "high"}}})
    by_name = {t.remote_name: t for t in tools}
    assert "delete_issue" not in by_name
    assert by_name["get_issue"].spec.permission.name == "READ" and not by_name["get_issue"].spec.requires_approval
    assert by_name["create_issue"].spec.permission.name == "MODIFY" and by_name["create_issue"].spec.requires_approval and by_name["create_issue"].spec.risk_level.value == "high"
    assert by_name["create_issue"].name == "mcp_fake_create_issue"
