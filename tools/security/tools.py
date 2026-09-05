"""Security tools: trivy / semgrep / gitleaks / checkov CLI wrappers + a built-in dependency-free secret scanner and manifest audit."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import yaml

from agent.audit.redaction import contains_secret
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command, which

_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".terraform", ".pytest_cache"}


class SecurityBackend(Protocol):
    def trivy(self, target: str, mode: str) -> dict[str, Any]: ...
    def semgrep(self, path: str) -> list[dict[str, Any]]: ...
    def gitleaks(self, path: str) -> list[dict[str, Any]]: ...
    def checkov(self, path: str) -> dict[str, Any]: ...


class CliSecurityBackend:
    def trivy(self, target: str, mode: str) -> dict[str, Any]:
        if not which("trivy"):
            raise ToolError("trivy is not installed", kind="unavailable", advice="install trivy or rely on the built-in scanners")
        out = run_command(["trivy", mode, "--format", "json", "--quiet", target], timeout=900)
        if not out.ok:
            raise ToolError(f"trivy failed: {out.stderr[:400]}", kind="unknown")
        data = json.loads(out.stdout or "{}")
        counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for r in data.get("Results", []):
            for v in r.get("Vulnerabilities", []) or []:
                counts[v.get("Severity", "LOW")] = counts.get(v.get("Severity", "LOW"), 0) + 1
        return counts

    def semgrep(self, path: str) -> list[dict[str, Any]]:
        if not which("semgrep"):
            raise ToolError("semgrep is not installed", kind="unavailable")
        out = run_command(["semgrep", "--config", "auto", "--json", "--quiet", path], timeout=900)
        data = json.loads(out.stdout or "{}")
        return [{"check": r.get("check_id"), "path": r.get("path"), "line": r.get("start", {}).get("line"), "severity": r.get("extra", {}).get("severity"),
                 "message": r.get("extra", {}).get("message", "")[:200]} for r in data.get("results", [])]

    def gitleaks(self, path: str) -> list[dict[str, Any]]:
        if not which("gitleaks"):
            raise ToolError("gitleaks is not installed", kind="unavailable")
        out = run_command(["gitleaks", "detect", "--source", path, "--no-git", "--report-format", "json", "--report-path", "-", "--exit-code", "0"], timeout=600)
        try:
            data = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            data = []
        return [{"file": f.get("File"), "line": f.get("StartLine"), "rule": f.get("RuleID")} for f in data]  # never include the secret itself

    def checkov(self, path: str) -> dict[str, Any]:
        if not which("checkov"):
            raise ToolError("checkov is not installed", kind="unavailable")
        out = run_command(["checkov", "-d", path, "-o", "json", "--quiet"], timeout=900)
        try:
            data = json.loads(out.stdout or "{}")
        except json.JSONDecodeError:
            return {"passed": 0, "failed": 0, "findings": [out.stdout[-500:]]}
        reports = data if isinstance(data, list) else [data]
        passed = sum(r.get("summary", {}).get("passed", 0) for r in reports)
        failed_checks = [c for r in reports for c in r.get("results", {}).get("failed_checks", [])]
        return {"passed": passed, "failed": len(failed_checks), "findings": [f"{c.get('check_id')}: {c.get('check_name')} ({c.get('file_path')})" for c in failed_checks[:30]]}


class MockSecurityBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def trivy(self, target, mode):
        return dict(self.world.security["trivy"]["image" if mode == "image" else "fs"])

    def semgrep(self, path):
        return list(self.world.security["semgrep"])

    def gitleaks(self, path):
        return list(self.world.security["gitleaks"])

    def checkov(self, path):
        return dict(self.world.security["checkov"])


def _path(args: dict[str, Any], ctx: ToolContext) -> Path:
    p = Path(args.get("path") or ctx.workspace or ctx.project_root or ".")
    if not p.exists():
        raise ToolError(f"path not found: {p}", kind="not_found")
    return p


@tool("sec_secret_scan", "Built-in secret scanner (no external tool needed): flags files containing credential-like strings; never prints the secret.",
      category="security", permissions=["filesystem.read"], input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
def sec_secret_scan(args, ctx):
    root = _path(args, ctx)
    findings = []
    files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file() and not any(part in _SKIP for part in f.relative_to(root).parts)]
    for f in files[:5000]:
        if f.suffix.lower() in (".png", ".jpg", ".gif", ".zip", ".gz", ".pyc", ".whl", ".jar"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if contains_secret(line) and not re.search(r"\*{6,}|example|placeholder|changeme|<[^>]+>|\$\{[A-Z_]+\}", line, re.I):
                findings.append({"file": str(f.relative_to(root)) if root.is_dir() else f.name, "line": n})
    return {"findings": findings, "files_scanned": len(files), "clean": not findings}


@tool("sec_k8s_manifest_audit", "Audit Kubernetes manifests for common security misconfigurations (privileged, root, missing limits/probes, latest tag, hostPath).",
      category="security", permissions=["filesystem.read"], input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
def sec_k8s_manifest_audit(args, ctx):
    root = _path(args, ctx)
    files = [root] if root.is_file() else [f for f in root.rglob("*.y*ml") if not any(part in _SKIP for part in f.relative_to(root).parts)]
    findings = []
    for f in files:
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8", errors="replace")))
        except yaml.YAMLError:
            findings.append({"file": f.name, "severity": "HIGH", "issue": "invalid YAML"})
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet", "Pod", "Job", "CronJob"):
                continue
            spec = doc.get("spec", {})
            pod = spec.get("template", {}).get("spec", spec) if doc.get("kind") != "CronJob" else spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
            name = f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"
            for c in pod.get("containers", []) or []:
                sc = c.get("securityContext", {}) or {}
                if sc.get("privileged"):
                    findings.append({"file": f.name, "object": name, "severity": "CRITICAL", "issue": f"container {c.get('name')} is privileged"})
                if not c.get("resources", {}).get("limits"):
                    findings.append({"file": f.name, "object": name, "severity": "MEDIUM", "issue": f"container {c.get('name')} has no resource limits"})
                if str(c.get("image", "")).endswith(":latest") or ":" not in str(c.get("image", "")):
                    findings.append({"file": f.name, "object": name, "severity": "MEDIUM", "issue": f"container {c.get('name')} uses an unpinned image tag"})
                if doc.get("kind") in ("Deployment", "StatefulSet") and not c.get("readinessProbe"):
                    findings.append({"file": f.name, "object": name, "severity": "LOW", "issue": f"container {c.get('name')} has no readinessProbe"})
                if sc.get("runAsUser") == 0:
                    findings.append({"file": f.name, "object": name, "severity": "HIGH", "issue": f"container {c.get('name')} runs as root"})
            for v in pod.get("volumes", []) or []:
                if "hostPath" in v:
                    findings.append({"file": f.name, "object": name, "severity": "HIGH", "issue": f"hostPath volume {v.get('name')}"})
            if pod.get("hostNetwork"):
                findings.append({"file": f.name, "object": name, "severity": "HIGH", "issue": "hostNetwork enabled"})
    high = [x for x in findings if x["severity"] in ("HIGH", "CRITICAL")]
    return {"findings": findings, "blocking": high, "files_scanned": len(files)}


@tool("sec_trivy_scan", "Scan an image (mode=image) or a filesystem/IaC path (mode=fs) with Trivy.", category="security", permissions=["security.scan"],
      input_schema={"type": "object", "properties": {"target": {"type": "string"}, "mode": {"type": "string"}}, "required": ["target"]}, timeout=900)
def sec_trivy_scan(args, ctx):
    counts = ctx.backend("security").trivy(args["target"], args.get("mode") or "image")
    return {"target": args["target"], "counts": counts, "blocking": counts.get("CRITICAL", 0) > 0}


@tool("sec_semgrep", "Static code analysis with semgrep (auto config).", category="security", permissions=["security.scan"],
      input_schema={"type": "object", "properties": {"path": {"type": "string"}}}, timeout=900)
def sec_semgrep(args, ctx):
    findings = ctx.backend("security").semgrep(str(_path(args, ctx)))
    return {"findings": findings, "blocking": [f for f in findings if str(f.get("severity", "")).upper() == "ERROR"]}


@tool("sec_gitleaks", "Secret scanning with gitleaks.", category="security", permissions=["security.scan"],
      input_schema={"type": "object", "properties": {"path": {"type": "string"}}}, timeout=600)
def sec_gitleaks(args, ctx):
    findings = ctx.backend("security").gitleaks(str(_path(args, ctx)))
    return {"findings": findings, "clean": not findings}


@tool("sec_checkov", "IaC scanning with checkov.", category="security", permissions=["security.scan"],
      input_schema={"type": "object", "properties": {"path": {"type": "string"}}}, timeout=900)
def sec_checkov(args, ctx):
    return ctx.backend("security").checkov(str(_path(args, ctx)))


def build_tools() -> list[Tool]:
    return [sec_secret_scan, sec_k8s_manifest_audit, sec_trivy_scan, sec_semgrep, sec_gitleaks, sec_checkov]
