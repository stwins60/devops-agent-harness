import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    for k in list(env):
        if k.startswith("DEVOPS_AGENT_"):
            env.pop(k)
    return subprocess.run([sys.executable, "-m", "apps.cli.main", *args], cwd=str(cwd or ROOT), capture_output=True, text=True, env=env, timeout=300)


def test_free_text_question(tmp_path):
    r = run_cli("--mock", "-q", "--project-root", str(tmp_path), "Why is my Kubernetes API deployment failing?")
    assert r.returncode == 0, r.stderr
    assert "CONCLUSION:" in r.stdout and "Probe port mismatch" in r.stdout and "# DevOps Task Report" in r.stdout


def test_jira_command_creates_pr_and_updates_ticket(tmp_path):
    r = run_cli("--mock", "--yes", "--project-root", str(tmp_path), "jira", "DEVOPS-382")
    assert r.returncode == 0, r.stderr
    assert "PR https://github.com/example-org/sample-app/pull/421" in r.stdout and "transition=In Review" in r.stdout
    assert (tmp_path / "tasks" / "DEVOPS-382" / "final-report.md").exists()


def test_incident_pauses_without_explicit_approval(tmp_path):
    r = run_cli("--mock", "--yes", "--project-root", str(tmp_path), "incident", "production API is returning 503")
    assert r.returncode == 0
    assert "waiting for approval" in r.stdout and "Paused" in r.stdout


def test_plan_and_dry_run(tmp_path):
    r = run_cli("--mock", "-q", "--project-root", str(tmp_path), "plan", "upgrade our Kubernetes worker nodes")
    assert r.returncode == 0 and "## Rollback" in r.stdout and "kubernetes-worker-node-upgrade" in r.stdout
    r2 = run_cli("--mock", "--dry-run", "--project-root", str(tmp_path), "fix", "DEVOPS-382")
    assert r2.returncode == 0 and "(dry-run)" in r2.stdout and "DRY-RUN" in (tmp_path / "tasks" / "DEVOPS-382" / "changes.md").read_text()


def test_listing_commands(tmp_path):
    assert "kubectl_apply" in run_cli("--mock", "tools", "list").stdout
    assert "kubernetes-crashloopbackoff" in run_cli("runbooks", "list").stdout
    assert "Runbook: kubernetes-crashloopbackoff" in run_cli("runbooks", "show", "kubernetes-crashloopbackoff").stdout
    assert "no tasks" in run_cli("--project-root", str(tmp_path), "tasks", "list").stdout
    assert run_cli("--project-root", str(tmp_path), "init").returncode == 0 and (tmp_path / ".agent" / "config.yaml").exists()


def test_help_and_bad_usage():
    assert run_cli().returncode == 2
    assert run_cli("jira").returncode == 2
    assert "--dry-run" in run_cli("--help").stdout
