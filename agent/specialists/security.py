"""Security specialist: scans code, IaC, manifests, images and secrets; can block high-risk changes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent.models import Diagnosis, Hypothesis, Plan, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class SecuritySpecialist(Specialist):
    name = "security-agent"
    description = "Runs secret, manifest, IaC, dependency and container scans and blocks changes with critical findings."
    domains = ["security"]
    keywords = ["cve", "vulnerability", "vulnerabilities", "scan", "secret", "secrets", "trivy", "semgrep", "gitleaks", "checkov", "tfsec", "rbac", "security", "iam"]

    def investigate(self, inv: Investigation) -> None:
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        if not repo:
            inv.log.fact("No workspace to scan.", source="security-agent")
            return
        self.scan(inv, repo, record=True)

    def scan(self, inv: Investigation, repo: Path, *, record: bool = False) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        secrets = self.call(inv, "sec_secret_scan", {"path": str(repo)}, purpose="built-in secret scan")
        if secrets.ok:
            n = len(secrets.output.get("findings", []))
            results.append(ValidationResult("secret scan (built-in)", n == 0, f"{n} finding(s) in {secrets.output.get('files_scanned')} files"))
            if record:
                inv.log.fact(f"Secret scan: {n} finding(s) across {secrets.output.get('files_scanned')} files.", source="sec_secret_scan", secret_findings=n)
        audit = self.call(inv, "sec_k8s_manifest_audit", {"path": str(repo)}, purpose="Kubernetes manifest security audit")
        if audit.ok:
            blocking = audit.output.get("blocking", [])
            findings = audit.output.get("findings", [])
            results.append(ValidationResult("kubernetes manifest audit", not blocking, f"{len(findings)} finding(s), {len(blocking)} blocking"))
            if record:
                inv.log.fact(f"Manifest audit: {len(findings)} finding(s), {len(blocking)} high/critical: " + "; ".join(f['issue'] for f in blocking[:3]), source="sec_k8s_manifest_audit",
                             manifest_blocking=len(blocking))
        for tool, args, label in (("sec_gitleaks", {"path": str(repo)}, "gitleaks"), ("sec_semgrep", {"path": str(repo)}, "semgrep"), ("sec_checkov", {"path": str(repo)}, "checkov")):
            res = self.call(inv, tool, args, purpose=f"{label} scan")
            if res.ok:
                if label == "checkov":
                    failed = res.output.get("failed", 0)
                    results.append(ValidationResult(label, True, f"{res.output.get('passed', 0)} passed, {failed} failed (advisory)"))
                else:
                    findings = res.output.get("findings", [])
                    blocking = res.output.get("blocking", []) if label == "semgrep" else findings
                    results.append(ValidationResult(label, not blocking, f"{len(findings)} finding(s)"))
            else:
                results.append(ValidationResult(label, True, f"not available ({res.failure_kind})", skipped=True))
        if any(c.kind == "file" and c.target.lower().startswith("dockerfile") for c in inv.task.changes) or (repo / "Dockerfile").exists() and record:
            tr = self.call(inv, "sec_trivy_scan", {"target": str(repo), "mode": "fs"}, purpose="trivy filesystem scan")
            if tr.ok:
                counts = tr.output.get("counts", {})
                results.append(ValidationResult("trivy fs", not tr.output.get("blocking"), f"critical={counts.get('CRITICAL', 0)} high={counts.get('HIGH', 0)}"))
            else:
                results.append(ValidationResult("trivy fs", True, f"not available ({tr.failure_kind})", skipped=True))
        return results

    def analyzers(self):
        return [("security.findings", _findings)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        return None

    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        return self.scan(inv, repo) if repo else []

    @staticmethod
    def blocks(results: list[ValidationResult]) -> list[str]:
        return [f"{r.name}: {r.detail}" for r in results if not r.passed and not r.skipped]


def _findings(log: EvidenceLog) -> list[Hypothesis]:
    n, b = log.get("secret_findings"), log.get("manifest_blocking")
    if n:
        return [Hypothesis(statement=f"{n} file(s) contain credential-like strings.", validation="secret scanner", status="confirmed", confidence=0.85)]
    if b:
        return [Hypothesis(statement=f"{b} high/critical Kubernetes security misconfiguration(s).", validation="manifest audit", status="confirmed", confidence=0.85)]
    if log.has("secret_findings", 0) and log.has("manifest_blocking", 0):
        return [Hypothesis(statement="No blocking security findings.", validation="scanners", status="confirmed", confidence=0.8)]
    return []
