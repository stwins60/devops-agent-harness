import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from adapters.openai.provider import OpenAICompatibleProvider
from agent.models import OperatingMode, TaskKind, TaskStatus
from agent.orchestrator.decider import ModelDecider
from agent.providers.base import MockProvider, ModelMessage, ModelRequest, NullProvider, extract_json
from agent.providers.factory import build_provider
from agent.specialists.base import Investigation
from agent.state.store import TaskState


def test_extract_json_handles_fences_and_noise():
    assert extract_json('thinking...\n```json\n{"action": "complete", "x": 1}\n```')["x"] == 1
    assert extract_json('{"a": {"b": 2}} trailing')["a"]["b"] == 2
    assert extract_json("no json here") is None


def test_mock_provider_scripting_and_tool_calls():
    p = MockProvider([{"tool": "kubectl_get", "args": {"kind": "pods"}}, "plain text", {"action": "complete", "summary": "done"}])
    r1 = p.complete(ModelRequest(system="s", messages=[ModelMessage("user", "hi")]))
    assert r1.tool_calls[0].name == "kubectl_get"
    assert p.complete(ModelRequest(system="s", messages=[])).text == "plain text"
    assert p.complete(ModelRequest(system="s", messages=[])).parsed_json()["action"] == "complete"
    assert p.complete(ModelRequest(system="s", messages=[])).parsed_json()["action"] == "complete"  # default after script


def test_factory_returns_null_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr("tools.shell.which", lambda name: None)
    monkeypatch.setattr("adapters.claude.provider.which", lambda name: None)
    monkeypatch.setattr("adapters.opencode.provider.which", lambda name: None)
    monkeypatch.setattr("adapters.copilot.provider.which", lambda name: None)
    p = build_provider("auto")
    assert isinstance(p, NullProvider) and not p.available()
    assert isinstance(build_provider("mock"), MockProvider)


class _FakeOpenAI(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        return

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        assert self.headers.get("Authorization") == "Bearer test-key"
        assert body["tools"][0]["function"]["name"] == "kubectl_get"
        payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call_1", "function": {"name": "kubectl_get", "arguments": "{\"kind\": \"pods\"}"}}]},
                                "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_openai_compatible_provider_against_fake_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        p = OpenAICompatibleProvider(model="gpt-test", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        assert p.available()
        resp = p.complete(ModelRequest(system="sys", messages=[ModelMessage("user", "hi")], tools=[{"name": "kubectl_get", "description": "d", "input_schema": {"type": "object"}}]))
        assert resp.tool_calls[0].name == "kubectl_get" and resp.tool_calls[0].arguments == {"kind": "pods"}
        assert resp.prompt_tokens == 12 and resp.provider == "openai"
    finally:
        server.shutdown()


def test_decider_runs_tools_through_policy_and_confirms_only_with_facts(make_harness):
    script = [
        {"tool": "kubectl_rollout_restart", "args": {"name": "api", "namespace": "production"}},   # blocked: read-only mode
        {"tool": "kubectl_get", "args": {"kind": "deployment", "name": "api", "namespace": "production"}},
        {"action": "complete", "summary": "s", "facts": ["deployment api has 0/3 ready"],
         "hypotheses": [{"statement": "probes point at the wrong port", "validation": "events", "status": "confirmed", "confidence": 0.8}],
         "recommendations": ["fix the probe port"]},
    ]
    h = make_harness(provider=MockProvider(script))
    task = TaskState(id="T-DEC", request="why is api failing?", mode=OperatingMode.READ_ONLY, environment=h.config.environment)
    h.store.save(task)
    inv = Investigation(task=task, harness=h, targets={"namespace": "production"})
    diag = ModelDecider(h).investigate(inv, [])
    assert diag and diag.conclusion == "probes point at the wrong port"
    tools = [c.tool for c in task.tool_calls]
    assert tools == ["kubectl_rollout_restart", "kubectl_get"] and not task.tool_calls[0].ok and task.tool_calls[1].ok
    assert h.world.mutations == []
    assert any(f.source.endswith("(model)") for f in inv.log.facts())


def test_decider_stops_on_repeated_calls_and_iteration_limit(make_harness):
    h = make_harness(provider=MockProvider([{"tool": "kubectl_get_nodes", "args": {}}] * 10))
    h.config.limits.max_repeated_calls = 2
    task = TaskState(id="T-LOOP", request="x", mode=OperatingMode.READ_ONLY, environment=h.config.environment)
    h.store.save(task)
    inv = Investigation(task=task, harness=h, targets={})
    assert ModelDecider(h).investigate(inv, []) is None
    assert any("repeatedly" in e for e in task.errors)


def test_decider_proposes_file_changes_from_model(make_harness, sample_repo):
    script = [{"action": "complete", "summary": "fix probe", "changes": [{"description": "fix port", "path": "k8s/deployment.yaml", "old": "port: 8000", "new": "port: 8080"}]}]
    h = make_harness(provider=MockProvider(script))
    task = TaskState(id="T-PLAN", request="fix", mode=OperatingMode.APPROVAL, environment=h.config.environment, workspace=str(sample_repo))
    h.store.save(task)
    inv = Investigation(task=task, harness=h, targets={})
    from agent.models import Diagnosis

    plan = ModelDecider(h).propose(inv, Diagnosis(problem="p", conclusion="probe port"))
    assert plan and plan.changes[0].tool == "fs_replace" and plan.changes[0].args["path"] == "k8s/deployment.yaml"
