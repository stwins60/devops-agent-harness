from agent.approvals.engine import (AllowlistHandler, ApprovalEngine, AutoApproveHandler, AutoDenyHandler, InteractiveHandler, RecordingHandler,
                                    build_handler)
from agent.models import ApprovalDecision, ApprovalRequest, RiskLevel


def req(op="kubectl_apply -f d.yaml", risk=RiskLevel.HIGH, env="production"):
    return ApprovalRequest(operation=op, description="apply", environment=env, risk=risk, resources=["Deployment/api"], expected_impact="rolling deployment",
                           rollback="kubectl rollout undo deployment/api", diff="--- a\n+++ b", plan="# plan", tool=op.split()[0])


def test_auto_deny_and_auto_approve():
    assert AutoDenyHandler().request(req()).decision == ApprovalDecision.DENY
    assert AutoApproveHandler().request(req()).decision == ApprovalDecision.APPROVE
    assert AutoApproveHandler().request(req(), explicit=True).decision == ApprovalDecision.DENY
    assert AutoApproveHandler(allow_explicit=True).request(req(), explicit=True).decision == ApprovalDecision.APPROVE


def test_allowlist_matches_tool_or_operation_glob():
    h = AllowlistHandler(["kubectl_apply", "git_push:fix/*"], fallback=AutoDenyHandler())
    assert h.request(req()).approved
    assert not h.request(req(op="terraform_apply")).approved


def test_interactive_prompt_supports_all_answers():
    answers = iter(["?", "d", "p", "r", "y"])
    outputs = []
    h = InteractiveHandler(input_fn=lambda prompt: next(answers), output_fn=outputs.append)
    out = h.request(req())
    assert out.approved
    joined = "\n".join(outputs)
    assert "--- a" in joined and "# plan" in joined and "rollout undo" in joined and "y=approve" in joined


def test_interactive_deny_skip_and_explicit_mismatch():
    assert InteractiveHandler(input_fn=lambda p: "n", output_fn=lambda s: None).request(req()).decision == ApprovalDecision.DENY
    assert InteractiveHandler(input_fn=lambda p: "s", output_fn=lambda s: None).request(req()).decision == ApprovalDecision.SKIP
    answers = iter(["y", "approve something-else"])
    out = InteractiveHandler(input_fn=lambda p: next(answers), output_fn=lambda s: None).request(req(), explicit=True)
    assert out.decision == ApprovalDecision.DENY
    answers = iter(["y", "approve kubectl_apply"])
    out = InteractiveHandler(input_fn=lambda p: next(answers), output_fn=lambda s: None).request(req(), explicit=True)
    assert out.approved


def test_interactive_eof_denies():
    def boom(prompt):
        raise EOFError

    assert InteractiveHandler(input_fn=boom, output_fn=lambda s: None).request(req()).decision == ApprovalDecision.DENY


def test_engine_records_every_decision():
    h = RecordingHandler([ApprovalDecision.DENY])
    e = ApprovalEngine(h)
    assert not e.ask(req()).approved
    assert e.ask(req()).approved  # default after script exhausted
    assert len(e.records) == 2 and e.records[0].decision == "deny" and e.records[0].request["operation"].startswith("kubectl_apply")


def test_build_handler_variants():
    assert isinstance(build_handler(interactive=False), AutoDenyHandler)
    assert isinstance(build_handler(interactive=False, auto_approve=True), AutoApproveHandler)
    assert isinstance(build_handler(interactive=True), InteractiveHandler)
    assert isinstance(build_handler(interactive=False, preapproved=["x"]), AllowlistHandler)


def test_render_mentions_missing_rollback():
    r = req()
    r.rollback = ""
    assert "NOT AVAILABLE" in r.render()
