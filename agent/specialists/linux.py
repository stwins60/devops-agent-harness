"""Linux specialist: CPU, memory, disk, services, journal, networking, kernel."""
from __future__ import annotations

import re
from typing import Optional

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class LinuxSpecialist(Specialist):
    name = "linux-agent"
    description = "Diagnoses Linux hosts: disk, memory, CPU, systemd services, journal, kernel, networking."
    domains = ["linux"]
    keywords = ["disk", "memory", "cpu", "systemd", "systemctl", "journalctl", "ssh", "host", "server", "filesystem", "mount", "cron", "kernel", "linux", "unit", "space"]

    def investigate(self, inv: Investigation) -> None:
        self.use_runbook(inv, inv.task.request, domain="linux")
        up = self.call(inv, "linux_uptime", {}, purpose="host uptime/load")
        if not up.ok:
            if up.failure_kind in ("network", "auth", "timeout", "unavailable"):
                inv.blocked = f"host unreachable: {up.error}"
            return
        inv.log.fact(f"Host uptime/load: {up.output['stdout'].strip()}", source="linux_uptime")
        df = self.call(inv, "linux_disk_usage", {}, purpose="filesystem usage")
        if df.ok:
            full = []
            for line in df.output["stdout"].splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 6 and parts[4].endswith("%"):
                    pct = int(parts[4].rstrip("%"))
                    if pct >= 90:
                        full.append((parts[5], pct))
            inv.log.fact("Filesystem usage: " + "; ".join(l.strip() for l in df.output["stdout"].splitlines()[1:]), source="linux_disk_usage", full_filesystems=full)
            for mount, pct in full:
                du = self.call(inv, "linux_dir_usage", {"path": mount}, purpose=f"largest directories under {mount}")
                if du.ok:
                    inv.log.fact(f"Largest directories under {mount}: " + "; ".join(du.output["stdout"].splitlines()[:3]), source=f"linux_dir_usage({mount})",
                                 largest_dirs=du.output["stdout"].splitlines()[:3])
                    top = du.output["stdout"].splitlines()[0].split("\t")[-1] if du.output["stdout"].strip() else None
                    if top:
                        big = self.call(inv, "linux_largest_files", {"path": top}, purpose=f"largest files in {top}")
                        if big.ok:
                            inv.log.fact(f"Largest files in {top}: " + "; ".join(big.output["stdout"].splitlines()[:3]), source=f"linux_largest_files({top})",
                                         largest_files=big.output["stdout"].splitlines()[:3])
        mem = self.call(inv, "linux_memory", {}, purpose="memory usage")
        if mem.ok:
            m = re.search(r"Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+(\d+)", mem.output["stdout"])
            if m:
                total, used, avail = int(m.group(1)), int(m.group(2)), int(m.group(4))
                inv.log.fact(f"Memory: {used}/{total} MB used, {avail} MB available.", source="linux_memory", mem_total=total, mem_available=avail)
        failed = self.call(inv, "linux_failed_units", {}, purpose="failed systemd units")
        if failed.ok:
            units = re.findall(r"^\W*\s*(\S+\.service)\s+loaded\s+failed", failed.output["stdout"], re.M)
            inv.log.fact(f"Failed systemd units: {units or 'none'}.", source="linux_failed_units", failed_units=units)
            unit = inv.target("unit") or (units[0] if units else None) or (inv.target("service") + ".service" if inv.target("service") else None)
            if unit:
                short = unit.replace(".service", "")
                st = self.call(inv, "linux_service_status", {"unit": short}, purpose=f"status of {unit}")
                if st.ok:
                    active = re.search(r"Active:\s+(.+)", st.output["stdout"])
                    inv.log.fact(f"Service {unit}: {active.group(1).strip() if active else st.output['stdout'][:120]}", source=f"linux_service_status({short})",
                                 unit=unit, unit_active=(active.group(1) if active else ""))
                j = self.call(inv, "linux_journal", {"unit": short, "lines": 50}, purpose=f"journal of {unit}")
                if j.ok:
                    lines = j.output["stdout"].splitlines()
                    errors = [l for l in lines if re.search(r"error|failed|no space|oom|killed|denied|refused", l, re.I)]
                    inv.log.fact(f"Journal of {unit} (last lines): " + " | ".join(lines[-3:]), source=f"linux_journal({short})", journal_errors=errors[:5])
        ports = self.call(inv, "linux_listening_ports", {}, purpose="listening ports")
        if ports.ok:
            listening = re.findall(r"LISTEN\s+\S*\s*[\d.:*\[\]]+:(\d+)", ports.output["stdout"])
            inv.log.fact(f"Listening TCP/UDP ports: {sorted(set(listening))}.", source="linux_listening_ports", listening_ports=sorted(set(listening)))
        dmesg = self.call(inv, "linux_dmesg", {}, purpose="kernel messages")
        if dmesg.ok:
            oom = [l for l in dmesg.output["stdout"].splitlines() if "out of memory" in l.lower() or "oom-killer" in l.lower()]
            if oom:
                inv.log.fact(f"Kernel OOM killer activity: {oom[-1]}", source="linux_dmesg", kernel_oom=True)

    def analyzers(self):
        return [("linux.disk_full", _disk_full), ("linux.service_failed", _service_failed), ("linux.oom", _oom), ("linux.healthy", _healthy)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        if not diagnosis.conclusion or "no fault" in diagnosis.conclusion.lower():
            return None
        plan = Plan(task_id=inv.task.id, title=f"Linux: {diagnosis.conclusion[:70]}", problem=diagnosis.problem, root_cause=diagnosis.conclusion,
                    evidence=[f.statement for f in diagnosis.facts][:10])
        if any(w in diagnosis.conclusion.lower() for w in ("disk", "filesystem", "% full", "enospc", "no space")):
            plan.changes.append(ProposedChange(description="Reclaim space: rotate/vacuum logs (journalctl --vacuum-size, logrotate -f) and remove the oversized log files after archiving",
                                               kind="command", target=inv.log.get("largest_files", ["/var/log"])[0] if inv.log.get("largest_files") else "/var/log",
                                               tool=None, risk=RiskLevel.HIGH, permission=PermissionLevel.DESTROY, rollback="NOT AVAILABLE for deleted logs; archive before deleting"))
            unit = inv.log.get("unit")
            if unit:
                plan.changes.append(ProposedChange(description=f"Restart {unit} after space is reclaimed", kind="command", target=unit, tool="linux_service_restart",
                                                   args={"unit": unit.replace('.service', '')}, risk=RiskLevel.HIGH, permission=PermissionLevel.DEPLOY,
                                                   rollback="not reversible (restart)", environment=inv.task.environment.value))
            plan.risks = ["deleting logs loses forensic data", "service restart causes a brief outage"]
            plan.rollback = ["restore archived logs if needed"]
            plan.validation = ["df -h shows < 80% on the affected filesystem", "systemctl status shows active (running)", "journal free of 'No space left on device'"]
            plan.risk_level = RiskLevel.HIGH
            plan.required_permissions = ["linux.service", "shell.execute"]
        elif "service" in diagnosis.conclusion.lower():
            unit = inv.log.get("unit")
            plan.changes.append(ProposedChange(description=f"Fix the failure cause in the journal, then restart {unit}", kind="command", target=unit or "service",
                                               tool="linux_service_restart" if unit else None, args={"unit": unit.replace('.service', '')} if unit else {},
                                               risk=RiskLevel.HIGH, permission=PermissionLevel.DEPLOY, rollback="not reversible (restart)"))
            plan.validation = ["systemctl is-active reports active"]
            plan.risk_level = RiskLevel.HIGH
        return plan if plan.changes else None


def _disk_full(log: EvidenceLog) -> list[Hypothesis]:
    full = log.get("full_filesystems") or []
    if not full:
        return []
    mount, pct = full[0]
    errors = " ".join(log.get("journal_errors") or [])
    confirmed = "no space left" in errors.lower() or pct >= 95
    big = log.get("largest_dirs") or []
    log.recommendation(f"Free space on {mount} (largest: {big[0] if big else 'unknown'}); add log rotation to prevent recurrence.")
    return [Hypothesis(statement=f"Filesystem {mount} is {pct}% full{'; the service fails with ENOSPC' if 'no space left' in errors.lower() else ''}.",
                       validation="df shows >= 95% or the journal contains 'No space left on device'.", status="confirmed" if confirmed else "unvalidated", confidence=0.95 if confirmed else 0.6)]


def _service_failed(log: EvidenceLog) -> list[Hypothesis]:
    units = log.get("failed_units") or []
    if not units or log.get("full_filesystems"):
        return []
    errors = log.get("journal_errors") or []
    return [Hypothesis(statement=f"Service {units[0]} is in failed state: {errors[0][:120] if errors else 'no error captured in journal'}.",
                       validation="journalctl shows the failure reason.", status="confirmed" if errors else "unvalidated", confidence=0.85 if errors else 0.5)]


def _oom(log: EvidenceLog) -> list[Hypothesis]:
    if log.get("kernel_oom"):
        return [Hypothesis(statement="Kernel OOM killer terminated processes (host memory exhausted).", validation="dmesg oom-killer entries.", status="confirmed", confidence=0.9)]
    return []


def _healthy(log: EvidenceLog) -> list[Hypothesis]:
    if not log.get("full_filesystems") and not log.get("failed_units") and not log.get("kernel_oom") and log.has("mem_total"):
        return [Hypothesis(statement="No host fault detected: disks below 90%, no failed units, no kernel OOM.", validation="df/systemctl/dmesg checks.", status="confirmed", confidence=0.8)]
    return []
