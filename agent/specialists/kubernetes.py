"""Kubernetes specialist.

Workflow: Deployment -> Pods -> Events -> Container status -> Logs -> Probes -> Resources -> Scheduling -> Networking -> Configuration.
Every conclusion is backed by evidence collected through tools.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models import Diagnosis, Hypothesis, PermissionLevel, Plan, ProposedChange, RiskLevel, TaskKind, ValidationResult
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class KubernetesSpecialist(Specialist):
    name = "kubernetes-agent"
    description = "Diagnoses and fixes Kubernetes workloads (pods, deployments, probes, resources, scheduling, networking, config)."
    domains = ["kubernetes"]
    keywords = ["kubernetes", "k8s", "kubectl", "pod", "pods", "deployment", "crashloop", "crashloopbackoff", "namespace", "helm", "ingress", "node",
                "nodes", "readiness", "liveness", "oomkilled", "imagepullbackoff", "replicas", "worker", "cluster"]

    # ------------------------------------------------------------------
    def investigate(self, inv: Investigation) -> None:
        ns = inv.target("namespace") or self.h.config.default_namespace
        inv.set_target("namespace", ns)
        name = inv.target("deployment") or inv.target("service")
        workload_words = re.search(r"\b(pod|pods|deployment|deployments|crash|crashing|crashloop|failing|fails|failed|ready|readiness|liveness|restart|restarting|"
                                   r"503|502|unavailable|rollout|image|oom|oomkilled|pending|error|errors|down|service|api|app|container|replicas?)\b",
                                   inv.task.request.lower())
        if not name and (inv.task.kind == TaskKind.PLAN or not workload_words):
            # change planning: inventory the cluster instead of hunting for a fault
            self.use_runbook(inv, inv.task.request, domain="kubernetes")
            self._inspect_nodes(inv)
            res = self.call(inv, "kubectl_get", {"kind": "deployments", "namespace": ns}, purpose="workload inventory for the change plan")
            if res.ok:
                items = res.output.get("items", [])
                inv.log.fact(f"Namespace '{ns}' runs {len(items)} deployment(s): " + ", ".join(f"{d['metadata']['name']} ({d.get('status', {}).get('readyReplicas') or 0}/{d.get('spec', {}).get('replicas') or 0} ready)" for d in items),
                             source=f"kubectl_get(deployments,-n {ns})", workloads=[d["metadata"]["name"] for d in items])
            return
        self.use_runbook(inv, inv.task.request + " pod deployment failing", domain="kubernetes")
        deployment = self._find_deployment(inv, ns, name)
        if deployment is None:
            if inv.task.kind == TaskKind.PLAN or re.search(r"\b(upgrade|node|nodes|worker)\b", inv.task.request.lower()):
                self._inspect_nodes(inv)
            return
        name = deployment["metadata"]["name"]
        inv.targets["deployment"] = name  # the discovered workload overrides whatever the request text suggested
        inv.targets.setdefault("service", name)
        self._inspect_deployment(inv, deployment)
        pods = self._inspect_pods(inv, ns, name, deployment)
        self._inspect_events(inv, ns, pods)
        self._inspect_logs(inv, ns, pods)
        self._inspect_probes_and_resources(inv, deployment)
        self._inspect_scheduling(inv, pods)
        self._inspect_networking(inv, ns, name)
        self._inspect_config(inv, ns, deployment)
        if inv.task.kind == TaskKind.PLAN or "upgrade" in inv.task.request.lower():
            self._inspect_nodes(inv)

    # -- steps ------------------------------------------------------------
    def _find_deployment(self, inv: Investigation, ns: str, name: Optional[str]) -> Optional[dict[str, Any]]:
        if name:
            res = self.call(inv, "kubectl_get", {"kind": "deployment", "name": name, "namespace": ns}, purpose="inspect target deployment")
            if res.ok:
                return res.output
            if res.failure_kind != "not_found":
                inv.blocked = f"cannot reach the cluster: {res.error}" if res.failure_kind in ("network", "auth", "permission") else None
                return None
            inv.log.fact(f"Deployment '{name}' was not found in namespace '{ns}'.", source=f"kubectl_get(deployment/{name})", not_found=name)
        res = self.call(inv, "kubectl_get", {"kind": "deployments", "namespace": ns}, purpose="list deployments in namespace")
        if not res.ok:
            if res.failure_kind in ("network", "auth", "permission", "unavailable"):
                inv.blocked = f"cannot inspect namespace '{ns}': {res.error}"
            return None
        items = res.output.get("items", []) if isinstance(res.output, dict) else []
        unhealthy = [d for d in items if (d.get("status", {}).get("readyReplicas") or 0) < (d.get("spec", {}).get("replicas") or 0)]
        if unhealthy:
            names = ", ".join(d["metadata"]["name"] for d in unhealthy)
            inv.log.fact(f"Namespace '{ns}' has {len(unhealthy)} deployment(s) with unavailable replicas: {names}.", source=f"kubectl_get(deployments,-n {ns})")
            return unhealthy[0]
        if items:
            inv.log.fact(f"All {len(items)} deployment(s) in namespace '{ns}' report all replicas ready.", source=f"kubectl_get(deployments,-n {ns})", all_healthy=True)
            if name is None and len(items) == 1:
                return items[0]
        return None

    def _inspect_deployment(self, inv: Investigation, dep: dict[str, Any]) -> None:
        name, ns = dep["metadata"]["name"], dep["metadata"].get("namespace", inv.target("namespace"))
        st, spec = dep.get("status", {}), dep.get("spec", {})
        image = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image")
        ready, want = st.get("readyReplicas") or 0, spec.get("replicas") or 0
        conds = {c.get("type"): c for c in st.get("conditions", [])}
        inv.log.fact(f"Deployment {ns}/{name}: {ready}/{want} replicas ready, image {image}, revision {dep['metadata'].get('annotations', {}).get('deployment.kubernetes.io/revision')}.",
                     source=f"kubectl_get(deployment/{name})", deployment=name, ready_replicas=ready, desired_replicas=want, image=image,
                     progressing_reason=conds.get("Progressing", {}).get("reason"))
        if ready < want:
            inv.log.fact(f"Deployment {name} is unavailable: condition Available={conds.get('Available', {}).get('status')} "
                         f"({conds.get('Available', {}).get('reason')}), Progressing={conds.get('Progressing', {}).get('reason')}.",
                         source=f"kubectl_get(deployment/{name})", unavailable=True)
        else:
            inv.log.fact(f"Deployment {name} is healthy: all {want} replicas ready and Available=True.", source=f"kubectl_get(deployment/{name})", deployment_healthy=True)
        hist = self.call(inv, "kubectl_rollout_history", {"kind": "deployment", "name": name, "namespace": ns}, purpose="rollout history")
        if hist.ok and hist.output.get("history"):
            last = hist.output["history"][-1]
            inv.log.fact(f"Latest rollout revision {last.get('revision')}: {last.get('change_cause') or 'no change-cause'}.",
                         source=f"kubectl_rollout_history({name})", rollout_history=hist.output["history"])

    def _inspect_pods(self, inv: Investigation, ns: str, name: str, dep: dict[str, Any]) -> list[dict[str, Any]]:
        labels = dep.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {"app": name}
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        res = self.call(inv, "kubectl_get", {"kind": "pods", "namespace": ns, "selector": selector}, purpose="list pods of the deployment")
        if not res.ok:
            return []
        pods = res.output.get("items", [])
        for p in pods:
            pname = p["metadata"]["name"]
            cs = (p.get("status", {}).get("containerStatuses") or [{}])[0]
            state = cs.get("state", {})
            waiting = state.get("waiting", {}).get("reason")
            last = cs.get("lastState", {}).get("terminated", {})
            desc = f"Pod {pname}: phase {p.get('status', {}).get('phase')}, ready={cs.get('ready')}, restarts={cs.get('restartCount', 0)}"
            if waiting:
                desc += f", waiting reason {waiting}"
            if last:
                desc += f", last exit code {last.get('exitCode')} ({last.get('reason')})"
            inv.log.fact(desc + ".", source=f"kubectl_get(pod/{pname})", pod=pname, phase=p.get("status", {}).get("phase"), ready=cs.get("ready"),
                         restarts=cs.get("restartCount", 0), waiting_reason=waiting, last_exit_code=last.get("exitCode"), last_reason=last.get("reason"),
                         node=p.get("spec", {}).get("nodeName"))
        if pods:
            inv.set_target("pod", pods[0]["metadata"]["name"])
        return pods

    def _inspect_events(self, inv: Investigation, ns: str, pods: list[dict[str, Any]]) -> None:
        res = self.call(inv, "kubectl_events", {"namespace": ns}, purpose="namespace events")
        if not res.ok:
            return
        pod_names = {p["metadata"]["name"] for p in pods}
        warnings = [e for e in res.output.get("events", []) if e.get("type") == "Warning" or e.get("reason") in ("Killing", "BackOff")]
        seen = set()
        for e in warnings:
            key = (e.get("reason"), (e.get("message") or "")[:80])
            if key in seen:
                continue
            seen.add(key)
            if pod_names and e.get("object", "").split("/")[-1] not in pod_names:
                continue
            inv.log.fact(f"Event {e.get('reason')} ({e.get('count')}x) on {e.get('object')}: {e.get('message')}", source=f"kubectl_events(-n {ns})",
                         event_reason=e.get("reason"), event_message=e.get("message"))

    def _inspect_logs(self, inv: Investigation, ns: str, pods: list[dict[str, Any]]) -> None:
        for p in pods[:1]:
            pname = p["metadata"]["name"]
            cs = (p.get("status", {}).get("containerStatuses") or [{}])[0]
            previous = bool(cs.get("restartCount", 0) > 0 and not cs.get("ready"))
            res = self.call(inv, "kubectl_logs", {"pod": pname, "namespace": ns, "tail": 50, "previous": False}, purpose="container logs")
            if res.ok:
                lines = [l for l in res.output.get("lines", []) if l.strip()]
                tail = lines[-6:]
                listening = next((l for l in lines if re.search(r"listen(ing)?\s+on\s+:?(\d+)|port\s+(\d+)", l, re.I)), None)
                port = None
                if listening:
                    m = re.search(r":(\d{2,5})\b|port\s+(\d{2,5})", listening, re.I)
                    port = int(next(g for g in m.groups() if g)) if m else None
                errors = [l for l in lines if re.search(r"error|exception|traceback|fatal|panic", l, re.I)]
                inv.log.fact(f"Logs of {pname} ({'previous container, ' if previous else ''}last {len(tail)} lines): " + " | ".join(tail),
                             source=f"kubectl_logs({pname})", log_lines=tail, log_errors=errors[:5], app_listen_port=port)
                if port:
                    inv.log.fact(f"Application log shows it listens on port {port}.", source=f"kubectl_logs({pname})", listen_port=port)

    def _inspect_probes_and_resources(self, inv: Investigation, dep: dict[str, Any]) -> None:
        containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for c in containers:
            ports = [p.get("containerPort") for p in c.get("ports", []) if p.get("containerPort")]
            probes = {}
            for kind in ("readinessProbe", "livenessProbe", "startupProbe"):
                pr = c.get(kind)
                if pr:
                    handler = pr.get("httpGet") or pr.get("tcpSocket") or pr.get("grpc") or {}
                    probes[kind] = {"port": handler.get("port"), "path": handler.get("path"), "failureThreshold": pr.get("failureThreshold"),
                                    "initialDelaySeconds": pr.get("initialDelaySeconds")}
            inv.log.fact(f"Container {c.get('name')}: containerPorts={ports}, probes=" + ", ".join(f"{k}->{v['port']}{v['path'] or ''}" for k, v in probes.items()) + ".",
                         source="kubectl_get(deployment spec)", container=c.get("name"), container_ports=ports, probes=probes, image=c.get("image"))
            res = c.get("resources", {}) or {}
            inv.log.fact(f"Container {c.get('name')} resources: requests={res.get('requests', {})}, limits={res.get('limits', {})}.",
                         source="kubectl_get(deployment spec)", limits=res.get("limits", {}), requests=res.get("requests", {}))
            env_refs = [e for e in c.get("env", []) if e.get("valueFrom")]
            if env_refs:
                inv.log.fact(f"Container {c.get('name')} references {len(env_refs)} ConfigMap/Secret key(s): " +
                             ", ".join(f"{e['name']}<-{list(e['valueFrom'].values())[0].get('name')}/{list(e['valueFrom'].values())[0].get('key')}" for e in env_refs) + ".",
                             source="kubectl_get(deployment spec)", env_refs=env_refs)
        top = self.call(inv, "kubectl_top_pods", {"namespace": inv.target("namespace")}, purpose="pod resource usage")
        if top.ok and top.output.get("pods"):
            usage = top.output["pods"][0]
            inv.log.fact(f"Pod {usage.get('name')} current usage: cpu {usage.get('cpu')}, memory {usage.get('memory')}.", source="kubectl_top_pods",
                         usage_memory=usage.get("memory"), usage_cpu=usage.get("cpu"))

    def _inspect_scheduling(self, inv: Investigation, pods: list[dict[str, Any]]) -> None:
        pending = [p for p in pods if p.get("status", {}).get("phase") == "Pending" and not p.get("spec", {}).get("nodeName")]
        if pending:
            self._inspect_nodes(inv)

    def _inspect_nodes(self, inv: Investigation) -> None:
        res = self.call(inv, "kubectl_get_nodes", {}, purpose="node inventory")
        if res.ok:
            nodes = res.output.get("nodes", [])
            versions = sorted({n.get("version") for n in nodes if n.get("version")})
            tainted = [n["name"] for n in nodes if n.get("taints")]
            not_ready = [n["name"] for n in nodes if not n.get("ready")]
            inv.log.fact(f"Cluster has {len(nodes)} node(s); kubelet versions {versions}; tainted nodes {tainted or 'none'}; not ready {not_ready or 'none'}.",
                         source="kubectl_get_nodes", nodes=nodes, node_versions=versions, tainted_nodes=tainted, not_ready_nodes=not_ready)

    def _inspect_networking(self, inv: Investigation, ns: str, name: str) -> None:
        svc = self.call(inv, "kubectl_get", {"kind": "service", "name": name, "namespace": ns}, purpose="service definition")
        if svc.ok:
            ports = svc.output.get("spec", {}).get("ports", [])
            inv.log.fact(f"Service {name}: selector {svc.output.get('spec', {}).get('selector')}, ports {[(p.get('port'), p.get('targetPort')) for p in ports]}.",
                         source=f"kubectl_get(service/{name})", service_target_ports=[p.get("targetPort") for p in ports])
        ep = self.call(inv, "kubectl_get", {"kind": "endpoints", "name": name, "namespace": ns}, purpose="service endpoints")
        if ep.ok:
            addrs = [a.get("ip") for s in ep.output.get("subsets", []) or [] for a in s.get("addresses", []) or []]
            inv.log.fact(f"Service {name} has {len(addrs)} ready endpoint(s).", source=f"kubectl_get(endpoints/{name})", endpoint_count=len(addrs))

    def _inspect_config(self, inv: Investigation, ns: str, dep: dict[str, Any]) -> None:
        refs = inv.log.get("env_refs") or []
        for ref in refs:
            src = ref["valueFrom"]
            kind = "configmap" if "configMapKeyRef" in src else "secret" if "secretKeyRef" in src else None
            if kind != "configmap":
                continue
            cm_name, key = src["configMapKeyRef"].get("name"), src["configMapKeyRef"].get("key")
            res = self.call(inv, "kubectl_get", {"kind": "configmap", "name": cm_name, "namespace": ns}, purpose="referenced ConfigMap")
            if res.ok:
                keys = list((res.output.get("data") or {}).keys())
                inv.log.fact(f"ConfigMap {cm_name} keys: {keys}; required key '{key}' {'present' if key in keys else 'MISSING'}.",
                             source=f"kubectl_get(configmap/{cm_name})", configmap=cm_name, configmap_keys=keys, missing_key=None if key in keys else key)
            elif res.failure_kind == "not_found":
                inv.log.fact(f"ConfigMap {cm_name} referenced by the deployment does not exist.", source=f"kubectl_get(configmap/{cm_name})", missing_configmap=cm_name)

    # ------------------------------------------------------------------
    def analyzers(self):
        return [("k8s.probe_port_mismatch", _probe_port_mismatch), ("k8s.oom_killed", _oom_killed), ("k8s.image_pull", _image_pull),
                ("k8s.unschedulable", _unschedulable), ("k8s.config_error", _config_error), ("k8s.app_crash", _app_crash),
                ("k8s.no_endpoints", _no_endpoints), ("k8s.healthy", _healthy)]

    # ------------------------------------------------------------------
    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        task = inv.task
        if not diagnosis.conclusion:
            return None
        ns, name = inv.target("namespace"), inv.target("deployment")
        plan = Plan(task_id=task.id, title=f"Fix {ns}/{name}: {diagnosis.conclusion[:80]}", problem=diagnosis.problem, root_cause=diagnosis.conclusion,
                    evidence=[f.statement for f in diagnosis.facts][:12], infrastructure=[f"Deployment {ns}/{name}"],
                    validation=["manifest consistency check (probe ports match container ports)", "kubectl rollout status after deploy", "pods Ready and endpoints populated"],
                    required_permissions=["filesystem.write", "git.write", "git.push", "github.write"])
        repo = Path(task.workspace) if task.workspace else None
        conclusion = diagnosis.conclusion.lower()
        if "probe" in conclusion and "port" in conclusion:
            probe_port = inv.log.get("probe_port")
            app_port = inv.log.get("container_port") or inv.log.get("listen_port")
            if repo and probe_port and app_port:
                changes = self._manifest_port_changes(inv, repo, int(probe_port), int(app_port))
                if changes:
                    plan.changes.extend(changes)
                    plan.files = sorted({c.target for c in changes})
                    plan.steps = ["update probe ports in the Kubernetes manifests", "run manifest tests / validation", "commit on a fix branch and open a PR",
                                  "after merge, GitOps/CI deploys the corrected manifest; verify rollout"]
                    plan.rollback = [f"git revert the fix commit / restore {', '.join(plan.files)}", f"kubectl rollout undo deployment/{name} -n {ns} if the change was deployed"]
                    plan.risks = ["low: manifest-only change; rollout replaces pods (rolling update)"]
                    plan.risk_level = RiskLevel.LOW
                    return plan
            manifest = self._patched_live_manifest(inv, probe_port, app_port)
            if manifest:
                plan.changes.append(ProposedChange(description=f"Patch live deployment {ns}/{name} probes to port {app_port}", kind="infrastructure",
                                                   target=f"deployment/{name}", tool="kubectl_apply", args={"manifest": manifest, "namespace": ns},
                                                   risk=RiskLevel.HIGH, permission=PermissionLevel.DEPLOY, rollback=f"kubectl rollout undo deployment/{name} -n {ns}",
                                                   environment=task.environment.value))
                plan.rollback = [f"kubectl rollout undo deployment/{name} -n {ns}"]
                plan.risks = ["rolling replacement of all pods in the deployment", "live patch diverges from git until the manifest repo is updated"]
                plan.risk_level = RiskLevel.HIGH
                plan.steps = ["apply patched manifest (approval required)", "watch rollout status", "update the manifest repository to match"]
                return plan
        if "oom" in conclusion:
            limit = (inv.log.get("limits") or {}).get("memory", "unknown")
            plan.changes.append(ProposedChange(description=f"Raise memory limit of container '{name}' (currently {limit}) after profiling; or fix the memory growth in the app",
                                               kind="file" if repo else "infrastructure", target="k8s/deployment.yaml (resources.limits.memory)", tool=None,
                                               risk=RiskLevel.MEDIUM, permission=PermissionLevel.MODIFY, rollback="restore the previous limit"))
            plan.steps = ["confirm working-set growth pattern (leak vs legitimate)", "raise limit or fix leak", "redeploy and observe memory"]
            plan.risks = ["higher memory reservation reduces bin-packing headroom on nodes"]
            plan.cost_notes = ["higher memory requests may require additional node capacity (estimate; no pricing data configured)"]
            plan.risk_level = RiskLevel.MEDIUM
            return plan
        if "image" in conclusion:
            image = inv.log.get("image")
            plan.changes.append(ProposedChange(description=f"Correct the image reference '{image}' (tag does not exist in the registry) in the manifest", kind="file",
                                               target="k8s/deployment.yaml (image)", tool=None, risk=RiskLevel.LOW, permission=PermissionLevel.MODIFY,
                                               rollback="restore previous image tag"))
            plan.steps = ["verify the intended tag exists in the registry", "update the manifest", "open a PR"]
            plan.risk_level = RiskLevel.LOW
            return plan
        if "schedul" in conclusion:
            plan.changes.append(ProposedChange(description="Reduce CPU/memory requests to fit node allocatable, add a toleration for the taint, or add node capacity", kind="infrastructure",
                                               target=f"deployment/{name} (resources / tolerations) or node group size", tool=None, risk=RiskLevel.MEDIUM,
                                               permission=PermissionLevel.DEPLOY, rollback="restore previous requests / node count"))
            plan.cost_notes = ["adding nodes increases compute cost (estimate; no pricing data configured)"]
            plan.risk_level = RiskLevel.MEDIUM
            return plan
        if "configmap" in conclusion or "config" in conclusion:
            key = inv.log.get("missing_key")
            plan.changes.append(ProposedChange(description=f"Add the missing key '{key}' to ConfigMap {inv.log.get('configmap')} (value must come from the service owner)", kind="infrastructure",
                                               target=f"configmap/{inv.log.get('configmap')}", tool=None, risk=RiskLevel.MEDIUM, permission=PermissionLevel.MODIFY,
                                               rollback="remove the key again"))
            plan.risk_level = RiskLevel.MEDIUM
            return plan
        return plan if plan.changes else None

    def _manifest_port_changes(self, inv: Investigation, repo: Path, probe_port: int, app_port: int) -> list[ProposedChange]:
        res = self.call(inv, "fs_search", {"pattern": rf"port:\s*{probe_port}\b", "glob": "*.y*ml"}, purpose="find manifests with the wrong probe port")
        changes: list[ProposedChange] = []
        if not res.ok:
            return changes
        for hit in res.output.get("hits", []):
            path = hit["file"]
            if any(c.target == path for c in changes):
                continue
            read = self.call(inv, "fs_read", {"path": path}, purpose="read manifest")
            if not read.ok:
                continue
            content = read.output["content"]
            try:
                docs = [d for d in yaml.safe_load_all(content) if isinstance(d, dict)]
            except yaml.YAMLError:
                continue
            if not any(d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet") for d in docs):
                continue
            old_lines = [l for l in content.splitlines() if re.search(rf"^\s*port:\s*{probe_port}\s*$", l)]
            if not old_lines:
                continue
            old = old_lines[0]
            new = old.replace(str(probe_port), str(app_port))
            change = self.file_change(f"Point readiness/liveness probes at port {app_port} in {path}", path, old, new)
            change.args["replace_all"] = True
            change.diff = f"--- a/{path}\n+++ b/{path}\n-{old}\n+{new}\n(x{len(old_lines)})"
            changes.append(change)
        return changes

    def _patched_live_manifest(self, inv: Investigation, probe_port: Any, app_port: Any) -> Optional[str]:
        ns, name = inv.target("namespace"), inv.target("deployment")
        res = self.call(inv, "kubectl_get", {"kind": "deployment", "name": name, "namespace": ns}, purpose="fetch live manifest for patching")
        if not res.ok or not app_port:
            return None
        dep = dict(res.output)
        dep.pop("status", None)
        dep["metadata"] = {k: v for k, v in dep["metadata"].items() if k in ("name", "namespace", "labels")}
        for c in dep["spec"]["template"]["spec"]["containers"]:
            for kind in ("readinessProbe", "livenessProbe", "startupProbe"):
                pr = c.get(kind)
                if pr:
                    for h in ("httpGet", "tcpSocket"):
                        if pr.get(h):
                            pr[h]["port"] = int(app_port)
        return yaml.safe_dump(dep, sort_keys=False)

    # ------------------------------------------------------------------
    def validate(self, inv: Investigation, plan: Plan) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        repo = Path(inv.task.workspace) if inv.task.workspace else None
        file_changes = [c for c in plan.changes if c.kind == "file" and c.applied]
        if file_changes and repo:
            results.append(self.manifest_consistency(inv, repo))
        infra = [c for c in plan.changes if c.kind == "infrastructure" and c.applied and c.tool == "kubectl_apply"]
        for c in infra:
            ns, name = inv.target("namespace"), inv.target("deployment")
            st = self.call(inv, "kubectl_rollout_status", {"kind": "deployment", "name": name, "namespace": ns}, purpose="verify rollout")
            ok = st.ok and "successfully rolled out" in str(st.output.get("status", ""))
            results.append(ValidationResult("kubectl rollout status", ok, str(st.output.get("status") if st.ok else st.error)[:200]))
            ep = self.call(inv, "kubectl_get", {"kind": "endpoints", "name": name, "namespace": ns}, purpose="verify endpoints")
            n = len([a for s in (ep.output.get("subsets", []) if ep.ok else []) for a in s.get("addresses", [])])
            results.append(ValidationResult("service endpoints populated", n > 0, f"{n} ready endpoint(s)"))
        return results

    def manifest_consistency(self, inv: Investigation, repo: Path) -> ValidationResult:
        problems: list[str] = []
        checked = 0
        for path in list(repo.rglob("*.yaml")) + list(repo.rglob("*.yml")):
            if any(part in (".git", ".venv", "node_modules") for part in path.relative_to(repo).parts):
                continue
            try:
                docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
            except yaml.YAMLError as exc:
                problems.append(f"{path.name}: invalid YAML ({exc})")
                continue
            for d in docs:
                if d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                    continue
                checked += 1
                for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                    ports = {p.get("containerPort") for p in c.get("ports", [])}
                    named = {p.get("name") for p in c.get("ports", [])}
                    for kind in ("readinessProbe", "livenessProbe", "startupProbe"):
                        pr = c.get(kind, {}) or {}
                        handler = pr.get("httpGet") or pr.get("tcpSocket") or {}
                        port = handler.get("port")
                        if port is not None and port not in ports and port not in named:
                            problems.append(f"{path.name}: {d['kind']}/{d['metadata'].get('name')} {kind} targets port {port} but containerPorts are {sorted(ports)}")
        return ValidationResult("manifest consistency (probe ports)", not problems, "; ".join(problems) if problems else f"{checked} workload manifest(s) consistent")


# ----------------------------------------------------------------------
# analyzers
# ----------------------------------------------------------------------
def _probe_port_mismatch(log: EvidenceLog) -> list[Hypothesis]:
    out = []
    for ev in log.find("probes"):
        ports = set(ev.data.get("container_ports") or [])
        probes = ev.data.get("probes") or {}
        for kind, pr in probes.items():
            port = pr.get("port")
            if port is None or isinstance(port, str) or port in ports:
                continue
            probe_failed = any("probe failed" in (e.data.get("event_message") or "").lower() for e in log.find("event_reason"))
            listen = log.get("listen_port")
            app_port = (sorted(ports)[0] if ports else None) or listen
            log.items.append(_ev("INFERENCE", f"{kind} targets port {port}, which is not a declared containerPort ({sorted(ports)}){' and the application logs show it listens on ' + str(listen) if listen else ''}.",
                                 probe_port=port, container_port=app_port, confidence=0.9))
            log.recommendation(f"Set {kind} port to {app_port} (the port the container actually serves) in the deployment manifest.")
            h = Hypothesis(statement=f"Probe port mismatch: {kind} checks port {port} but the container listens on {app_port}; kubelet kills/never readies the pod.",
                           validation="Confirm probe-failure events (Unhealthy: dial tcp ...:port connection refused) and that the app log/containerPort agree on the real port.",
                           status="confirmed" if probe_failed else "unvalidated", confidence=0.95 if probe_failed else 0.6)
            out.append(h)
            break
    return out


def _oom_killed(log: EvidenceLog) -> list[Hypothesis]:
    out = []
    for ev in log.find("last_exit_code", 137):
        reason = ev.data.get("last_reason")
        limit = (log.get("limits") or {}).get("memory")
        usage = log.get("usage_memory")
        if reason == "OOMKilled":
            log.recommendation(f"Raise the memory limit above the observed working set (limit {limit}, usage {usage}) or fix the memory growth; re-profile before changing limits.")
            out.append(Hypothesis(statement=f"Container OOMKilled: exit 137 with termination reason OOMKilled; limit {limit}, usage {usage}.",
                                  validation="Termination reason OOMKilled present in container lastState.", status="confirmed", confidence=0.95))
        else:
            out.append(Hypothesis(statement=f"Container was killed with exit 137 (SIGKILL); possible OOM kill (limit {limit}, usage {usage}) or liveness-probe kill.",
                                  validation="Check container lastState.terminated.reason for OOMKilled and node dmesg for oom-killer; check Killing events for probe failures.",
                                  status="rejected" if log.has("event_reason", "Killing") else "unvalidated", confidence=0.4))
        break
    return out


def _image_pull(log: EvidenceLog) -> list[Hypothesis]:
    for ev in log.items:
        if ev.data.get("waiting_reason") in ("ImagePullBackOff", "ErrImagePull"):
            msgs = [e.data.get("event_message") for e in log.find("event_reason") if e.data.get("event_message")]
            detail = next((m for m in msgs if "pull image" in m.lower() or "manifest" in m.lower()), "")
            image = log.get("image")
            confirmed = bool(detail)
            log.recommendation(f"Fix the image reference {image}: verify the tag exists in the registry and that the node has pull credentials.")
            return [Hypothesis(statement=f"Image pull failure for {image}: {detail or 'kubelet cannot pull the image'}.",
                               validation="Failed/ErrImagePull events with the registry error message.", status="confirmed" if confirmed else "unvalidated",
                               confidence=0.95 if confirmed else 0.6)]
    return []


def _unschedulable(log: EvidenceLog) -> list[Hypothesis]:
    for ev in log.find("event_reason", "FailedScheduling"):
        msg = ev.data.get("event_message") or ""
        reasons = []
        if "insufficient cpu" in msg.lower():
            reasons.append("insufficient CPU on nodes")
        if "insufficient memory" in msg.lower():
            reasons.append("insufficient memory on nodes")
        if "taint" in msg.lower():
            reasons.append("untolerated node taint")
        log.recommendation("Lower resource requests, add tolerations/affinity, or scale the node group so pods fit.")
        return [Hypothesis(statement=f"Pods cannot be scheduled: {', '.join(reasons) or msg[:120]}.", validation="FailedScheduling event with scheduler reason.",
                           status="confirmed", confidence=0.95)]
    return []


def _config_error(log: EvidenceLog) -> list[Hypothesis]:
    for ev in log.items:
        if ev.data.get("waiting_reason") == "CreateContainerConfigError":
            key, cm = log.get("missing_key"), log.get("configmap") or log.get("missing_configmap")
            msg = next((e.data.get("event_message") for e in log.find("event_reason", "Failed") if "configmap" in (e.data.get("event_message") or "").lower()), "")
            confirmed = bool(key or log.get("missing_configmap") or msg)
            log.recommendation(f"Add key '{key}' to ConfigMap {cm} (or fix the reference) before the pods can start.")
            return [Hypothesis(statement=f"Container config error: required ConfigMap key '{key}' is missing from {cm}." if key else f"Container config error: {msg or 'ConfigMap/Secret reference cannot be resolved'}",
                               validation="Failed event 'couldn't find key' and ConfigMap contents.", status="confirmed" if confirmed else "unvalidated", confidence=0.95 if confirmed else 0.5)]
    return []


def _app_crash(log: EvidenceLog) -> list[Hypothesis]:
    for ev in log.items:
        if ev.data.get("waiting_reason") == "CrashLoopBackOff" and ev.data.get("last_exit_code") not in (137, None) and not log.has("probe_port"):
            errors = log.get("log_errors") or []
            return [Hypothesis(statement=f"Application exits with code {ev.data.get('last_exit_code')} shortly after start (application error): {errors[0][:120] if errors else 'no error line captured'}.",
                               validation="Application error/traceback in container logs (--previous) matching the exit code.", status="confirmed" if errors else "unvalidated",
                               confidence=0.85 if errors else 0.5)]
    return []


def _no_endpoints(log: EvidenceLog) -> list[Hypothesis]:
    if log.has("endpoint_count", 0) and log.get("unavailable"):
        log.inference("The Service has no ready endpoints because no pod passes readiness, so the ingress/load balancer returns 503 for this service.", confidence=0.9)
    return []


def _healthy(log: EvidenceLog) -> list[Hypothesis]:
    if (log.get("all_healthy") or log.get("deployment_healthy")) and not log.get("unavailable"):
        return [Hypothesis(statement="No workload fault detected: all deployments report all replicas ready.", validation="readyReplicas == replicas for every deployment.",
                           status="confirmed", confidence=0.9)]
    return []


def _ev(kind: str, statement: str, confidence: float = 0.8, **data: Any):
    from agent.models import Evidence, EvidenceKind

    return Evidence(EvidenceKind(kind), statement, source="analyzer", data=data, confidence=confidence)
