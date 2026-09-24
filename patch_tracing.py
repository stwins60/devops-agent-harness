"""Wire tracing into harness, executor and audit logger."""
import re

# ── 1. harness.py ────────────────────────────────────────────────────────────
txt = open("agent/harness.py", encoding="utf-8").read()
txt = txt.replace(
    "echo_audit: bool = False) -> None:",
    "echo_audit: bool = False, tracer=None) -> None:"
)
txt = re.sub(
    r"(self\.audit = AuditLogger\([^\n]+\))\r?\n(\s+self\.policy)",
    r"\1\n        self.tracer = tracer if tracer is not None else build_tracer(config)\n        self.audit.set_tracer(self.tracer)\n\2",
    txt
)
txt = txt.replace(
    "self.backends, self.store)",
    "self.backends, self.store, tracer=self.tracer)"
