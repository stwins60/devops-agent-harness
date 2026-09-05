"""Kubernetes tools: kubectl-backed real backend + MockWorld backend."""
from __future__ import annotations

import json
from typing import Any, Optional, Protocol

import yaml

from agent.models import PermissionLevel, RiskLevel, ToolResult, ToolSpec
from tools.base import Tool, ToolContext, ToolError, tool
from tools.mock.world import MockWorld
from tools.shell import run_command


class KubernetesBackend(Protocol):
    def current_context(self) -> str: ...
    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]: ...
    def list(self, kind: str, namespace: Optional[str], selector: Optional[str] = None) -> list[dict[str, Any]]: ...
    def describe(self, kind: str, name: str, namespace: str) -> str: ...
    def logs(self, pod: str, namespace: str, container: Optional[str] = None, previous: bool = False, tail: int = 200) -> str: ...
    def events(self, namespace: str, involved: Optional[str] = None) -> list[dict[str, Any]]: ...
    def top_pods(self, namespace: str) -> list[dict[str, Any]]: ...
    def nodes(self) -> list[dict[str, Any]]: ...
    def rollout_history(self, kind: str, name: str, namespace: str) -> list[dict[str, Any]]: ...
    def apply(self, manifest: str, namespace: Optional[str], dry_run: bool) -> str: ...
    def delete(self, kind: str, name: str, namespace: str) -> str: ...
    def rollout(self, action: str, kind: str, name: str, namespace: str) -> str: ...
    def diff(self, manifest: str, namespace: Optional[str]) -> str: ...


class KubectlBackend:
    def __init__(self, context: Optional[str] = None, timeout: int = 60) -> None:
        self.context = context
        self.timeout = timeout

    def _run(self, *args: str, input_text: Optional[str] = None, timeout: Optional[int] = None) -> str:
        argv = ["kubectl"]
        if self.context:
            argv += ["--context", self.context]
        argv += list(args)
        out = run_command(argv, timeout=timeout or self.timeout, input_text=input_text, env_passthrough=("KUBECONFIG",))
        if not out.ok:
            msg = (out.stderr or out.stdout).strip()
            kind = "network" if "connection refused" in msg.lower() or "unable to connect" in msg.lower() or "dial tcp" in msg.lower() else \
                   "permission" if "forbidden" in msg.lower() else "not_found" if "notfound" in msg.lower() or "not found" in msg.lower() else \
                   "auth" if "unauthorized" in msg.lower() else "timeout" if out.timed_out else "unknown"
            raise ToolError(f"kubectl {' '.join(args[:3])} failed: {msg[:500]}", kind=kind)
        return out.stdout

    def _json(self, *args: str) -> Any:
        return json.loads(self._run(*args, "-o", "json") or "{}")

    def current_context(self) -> str:
        return self._run("config", "current-context").strip()

    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        return self._json("get", kind, name, "-n", namespace)

    def list(self, kind: str, namespace: Optional[str], selector: Optional[str] = None) -> list[dict[str, Any]]:
        args = ["get", kind] + (["-n", namespace] if namespace else ["--all-namespaces"])
        if selector:
            args += ["-l", selector]
        return list(self._json(*args).get("items", []))

    def describe(self, kind: str, name: str, namespace: str) -> str:
        return self._run("describe", kind, name, "-n", namespace)

    def logs(self, pod: str, namespace: str, container: Optional[str] = None, previous: bool = False, tail: int = 200) -> str:
        args = ["logs", pod, "-n", namespace, f"--tail={tail}"]
        if container:
            args += ["-c", container]
        if previous:
            args.append("--previous")
        return self._run(*args)

    def events(self, namespace: str, involved: Optional[str] = None) -> list[dict[str, Any]]:
        args = ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]
        if involved:
            args += ["--field-selector", f"involvedObject.name={involved}"]
        return list(self._json(*args).get("items", []))

    def top_pods(self, namespace: str) -> list[dict[str, Any]]:
        out = self._run("top", "pods", "-n", namespace, "--no-headers")
        rows = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append({"name": parts[0], "cpu": parts[1], "memory": parts[2]})
        return rows

    def nodes(self) -> list[dict[str, Any]]:
        return list(self._json("get", "nodes").get("items", []))

    def rollout_history(self, kind: str, name: str, namespace: str) -> list[dict[str, Any]]:
        out = self._run("rollout", "history", f"{kind}/{name}", "-n", namespace)
        rows = []
        for line in out.splitlines()[2:]:
            parts = line.split(None, 1)
            if parts and parts[0].isdigit():
                rows.append({"revision": int(parts[0]), "change_cause": parts[1] if len(parts) > 1 else ""})
        return rows

    def apply(self, manifest: str, namespace: Optional[str], dry_run: bool) -> str:
        args = ["apply", "-f", "-"] + (["-n", namespace] if namespace else []) + (["--dry-run=server"] if dry_run else [])
        return self._run(*args, input_text=manifest, timeout=180)

    def delete(self, kind: str, name: str, namespace: str) -> str:
        return self._run("delete", kind, name, "-n", namespace, timeout=180)

    def rollout(self, action: str, kind: str, name: str, namespace: str) -> str:
        return self._run("rollout", action, f"{kind}/{name}", "-n", namespace, timeout=300)

    def diff(self, manifest: str, namespace: Optional[str]) -> str:
        argv = ["kubectl"] + (["--context", self.context] if self.context else []) + ["diff", "-f", "-"] + (["-n", namespace] if namespace else [])
        out = run_command(argv, timeout=self.timeout, input_text=manifest, env_passthrough=("KUBECONFIG",))
        if out.returncode not in (0, 1):  # 1 = differences found
            raise ToolError(f"kubectl diff failed: {out.stderr[:400]}", kind="unknown")
        return out.stdout


class MockKubernetesBackend:
    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def _check(self) -> None:
        if self.world.flags.get("k8s_unreachable"):
            raise ToolError("The connection to the server mock-cluster:6443 was refused - did you specify the right host or port? dial tcp: connection refused", kind="network")
        if self.world.flags.get("permission_denied"):
            raise ToolError('Error from server (Forbidden): pods is forbidden: User "devops-agent" cannot list resource "pods"', kind="permission")

    def current_context(self) -> str:
        return self.world.k8s["context"]

    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        self._check()
        kind = _norm_kind(kind)
        coll = self.world.k8s.get(kind, {})
        if kind == "pods":
            for p in self.world.k8s["pods"].get(namespace, []):
                if p["metadata"]["name"] == name:
                    return p
            raise ToolError(f'Error from server (NotFound): pods "{name}" not found', kind="not_found")
        if kind == "nodes":
            for n in self.world.k8s["nodes"]:
                if n["metadata"]["name"] == name:
                    return n
            raise ToolError(f'nodes "{name}" not found', kind="not_found")
        item = (coll.get(namespace) or {}).get(name) if isinstance(coll, dict) else None
        if item is None:
            raise ToolError(f'Error from server (NotFound): {kind} "{name}" not found in namespace {namespace}', kind="not_found")
        return item

    def list(self, kind: str, namespace: Optional[str], selector: Optional[str] = None) -> list[dict[str, Any]]:
        self._check()
        kind = _norm_kind(kind)
        if kind == "nodes":
            return list(self.world.k8s["nodes"])
        if kind == "namespaces":
            return [{"metadata": {"name": n}} for n in self.world.k8s["namespaces"]]
        if kind == "events":
            return self.events(namespace or "default")
        coll = self.world.k8s.get(kind, {})
        namespaces = [namespace] if namespace else list(coll.keys())
        items: list[dict[str, Any]] = []
        for ns in namespaces:
            entries = coll.get(ns, [] if kind == "pods" else {})
            values = entries if isinstance(entries, list) else list(entries.values())
            for v in values:
                if selector:
                    labels = v.get("metadata", {}).get("labels", {})
                    if not all(labels.get(k) == val for k, val in (kv.split("=", 1) for kv in selector.split(","))):
                        continue
                items.append(v)
        return items

    def describe(self, kind: str, name: str, namespace: str) -> str:
        obj = self.get(kind, name, namespace)
        events = [e for e in self.events(namespace) if e["involvedObject"]["name"] == name]
        text = yaml.safe_dump(obj, sort_keys=False)
        if events:
            text += "\nEvents:\n" + "\n".join(f"  {e['type']}  {e['reason']}  {e['count']}x  {e['message']}" for e in events)
        return text

    def logs(self, pod: str, namespace: str, container: Optional[str] = None, previous: bool = False, tail: int = 200) -> str:
        self._check()
        key = (namespace, pod)
        if key not in self.world.k8s["logs"]:
            if not any(p["metadata"]["name"] == pod for p in self.world.k8s["pods"].get(namespace, [])):
                raise ToolError(f'pods "{pod}" not found', kind="not_found")
            return ""
        text = self.world.k8s["logs"][key]
        return "\n".join(text.splitlines()[-tail:])

    def events(self, namespace: str, involved: Optional[str] = None) -> list[dict[str, Any]]:
        self._check()
        evs = self.world.k8s["events"].get(namespace, [])
        return [e for e in evs if not involved or e["involvedObject"]["name"] == involved]

    def top_pods(self, namespace: str) -> list[dict[str, Any]]:
        self._check()
        return list(self.world.k8s["top"].get(namespace, []))

    def nodes(self) -> list[dict[str, Any]]:
        self._check()
        return list(self.world.k8s["nodes"])

    def rollout_history(self, kind: str, name: str, namespace: str) -> list[dict[str, Any]]:
        self._check()
        return list(self.world.k8s["rollout_history"].get(f"{namespace}/{name}", []))

    def apply(self, manifest: str, namespace: Optional[str], dry_run: bool) -> str:
        self._check()
        docs = [d for d in yaml.safe_load_all(manifest) if d]
        outputs = []
        for doc in docs:
            kind = _norm_kind(doc.get("kind", ""))
            name = doc.get("metadata", {}).get("name", "")
            ns = doc.get("metadata", {}).get("namespace") or namespace or "default"
            if dry_run:
                outputs.append(f"{doc.get('kind', '').lower()}/{name} configured (server dry run)")
                continue
            if kind == "deployments":
                self.world.k8s["deployments"].setdefault(ns, {})
                current = self.world.k8s["deployments"][ns].get(name)
                self.world.k8s["deployments"][ns][name] = doc
                doc.setdefault("status", {})
                self._simulate_rollout(ns, name, doc, current)
                self.world.record("kubectl_apply", kind="Deployment", name=name, namespace=ns)
                outputs.append(f"deployment.apps/{name} configured")
            else:
                self.world.k8s.setdefault(kind, {}).setdefault(ns, {})[name] = doc
                self.world.record("kubectl_apply", kind=doc.get("kind"), name=name, namespace=ns)
                outputs.append(f"{doc.get('kind', '').lower()}/{name} configured")
        return "\n".join(outputs)

    def _simulate_rollout(self, ns: str, name: str, doc: dict[str, Any], previous: Optional[dict[str, Any]]) -> None:
        c = doc["spec"]["template"]["spec"]["containers"][0]
        ports = {p.get("containerPort") for p in c.get("ports", [])}
        probe_ports = {pr.get("httpGet", {}).get("port") or pr.get("tcpSocket", {}).get("port") for pr in (c.get("readinessProbe"), c.get("livenessProbe")) if pr}
        healthy = all(p in ports or isinstance(p, str) for p in probe_ports)
        if self.world.flags.get("partial_deploy"):
            healthy = False
        replicas = int(doc["spec"].get("replicas", 3))
        ready = replicas if healthy else (1 if self.world.flags.get("partial_deploy") else 0)
        doc["status"] = {"replicas": replicas, "readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": replicas,
                         "unavailableReplicas": replicas - ready, "observedGeneration": 8,
                         "conditions": [{"type": "Available", "status": "True" if ready == replicas else "False"}]}
        doc["metadata"].setdefault("annotations", {})["deployment.kubernetes.io/revision"] = "8"
        from tools.mock.world import _pod

        self.world.k8s["pods"][ns] = [_pod(f"{name}-8b7c6d5e4-{s}", phase="Running", ready=healthy, restarts=0) for s in ("n1", "n2", "n3")[:replicas]]
        if healthy:
            self.world.k8s["endpoints"].setdefault(ns, {})[name] = {"metadata": {"name": name}, "subsets": [{"addresses": [{"ip": "10.0.1.31"}], "ports": [{"port": 8080}]}]}
            self.world.k8s["events"][ns] = []
        self.world.k8s["rollout_history"].setdefault(f"{ns}/{name}", []).append({"revision": 8, "change_cause": "kubectl apply (devops-agent)"})
        self.world.k8s.setdefault("previous_deployments", {})[f"{ns}/{name}"] = previous

    def delete(self, kind: str, name: str, namespace: str) -> str:
        self._check()
        kind = _norm_kind(kind)
        coll = self.world.k8s.get(kind, {})
        if isinstance(coll, dict) and name in (coll.get(namespace) or {}):
            del coll[namespace][name]
            self.world.record("kubectl_delete", kind=kind, name=name, namespace=namespace)
            return f'{kind[:-1]} "{name}" deleted'
        raise ToolError(f'{kind} "{name}" not found', kind="not_found")

    def rollout(self, action: str, kind: str, name: str, namespace: str) -> str:
        self._check()
        dep = self.get(kind, name, namespace)
        if action == "undo":
            prev = self.world.k8s.get("previous_deployments", {}).get(f"{namespace}/{name}")
            if prev is not None:
                self.world.k8s["deployments"][namespace][name] = prev
                self._simulate_rollout(namespace, name, prev, dep)
            else:
                # undo the scenario break: restore probes to container port
                c = dep["spec"]["template"]["spec"]["containers"][0]
                port = c["ports"][0]["containerPort"]
                for pr in ("readinessProbe", "livenessProbe"):
                    if c.get(pr, {}).get("httpGet"):
                        c[pr]["httpGet"]["port"] = port
                self._simulate_rollout(namespace, name, dep, None)
            self.world.record("kubectl_rollout_undo", name=name, namespace=namespace)
            return f"deployment.apps/{name} rolled back"
        if action == "restart":
            self._simulate_rollout(namespace, name, dep, dep)
            self.world.record("kubectl_rollout_restart", name=name, namespace=namespace)
            return f"deployment.apps/{name} restarted"
        if action == "status":
            st = dep.get("status", {})
            if st.get("readyReplicas", 0) == st.get("replicas", 0):
                return f'deployment "{name}" successfully rolled out'
            return f'Waiting for deployment "{name}" rollout to finish: {st.get("readyReplicas", 0)} of {st.get("replicas", 0)} updated replicas are available...'
        raise ToolError(f"unsupported rollout action '{action}'", kind="invalid")

    def diff(self, manifest: str, namespace: Optional[str]) -> str:
        self._check()
        docs = [d for d in yaml.safe_load_all(manifest) if d]
        out = []
        for doc in docs:
            kind = _norm_kind(doc.get("kind", ""))
            name = doc["metadata"]["name"]
            ns = doc["metadata"].get("namespace") or namespace or "default"
            try:
                current = self.get(kind, name, ns)
            except ToolError:
                out.append(f"+ {doc.get('kind')}/{name} (new)")
                continue
            import difflib

            a = yaml.safe_dump({"spec": current.get("spec")}, sort_keys=True).splitlines(keepends=True)
            b = yaml.safe_dump({"spec": doc.get("spec")}, sort_keys=True).splitlines(keepends=True)
            out.append("".join(difflib.unified_diff(a, b, fromfile=f"live/{name}", tofile=f"manifest/{name}")))
        return "\n".join(out)


def _norm_kind(kind: str) -> str:
    k = kind.lower().split("/")[-1]
    aliases = {"deploy": "deployments", "deployment": "deployments", "deployments": "deployments", "po": "pods", "pod": "pods", "pods": "pods",
               "svc": "services", "service": "services", "services": "services", "ep": "endpoints", "endpoint": "endpoints", "endpoints": "endpoints",
               "ing": "ingress", "ingress": "ingress", "ingresses": "ingress", "cm": "configmaps", "configmap": "configmaps", "configmaps": "configmaps",
               "no": "nodes", "node": "nodes", "nodes": "nodes", "ns": "namespaces", "namespace": "namespaces", "namespaces": "namespaces",
               "ev": "events", "event": "events", "events": "events", "hpa": "hpa", "horizontalpodautoscaler": "hpa", "rs": "replicasets", "replicaset": "replicasets"}
    return aliases.get(k, k if k.endswith("s") else k + "s")


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------
def _ns(args: dict[str, Any], ctx: ToolContext) -> str:
    return str(args.get("namespace") or (getattr(ctx.config, "default_namespace", None) if ctx.config else None) or "default")


@tool("kubectl_current_context", "Show the active kubectl context (used to verify environment identity).", category="kubernetes",
      permissions=["kubernetes.read"])
def kubectl_current_context(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"context": ctx.backend("kubernetes").current_context()}


@tool("kubectl_get", "Get a Kubernetes resource as JSON (kind + name) or list a kind in a namespace.", category="kubernetes",
      input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"},
                                                     "selector": {"type": "string"}, "all_namespaces": {"type": "boolean"}}, "required": ["kind"]},
      permissions=["kubernetes.read"])
def kubectl_get(args: dict[str, Any], ctx: ToolContext) -> Any:
    be = ctx.backend("kubernetes")
    if args.get("name"):
        return be.get(args["kind"], args["name"], _ns(args, ctx))
    items = be.list(args["kind"], None if args.get("all_namespaces") else _ns(args, ctx), args.get("selector"))
    return {"kind": args["kind"], "count": len(items), "items": items}


@tool("kubectl_describe", "Describe a Kubernetes resource including its events.", category="kubernetes",
      input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
                    "required": ["kind", "name"]}, permissions=["kubernetes.read"])
def kubectl_describe(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"description": ctx.backend("kubernetes").describe(args["kind"], args["name"], _ns(args, ctx))}


@tool("kubectl_logs", "Fetch container logs for a pod (optionally the previous crashed container).", category="kubernetes",
      input_schema={"type": "object", "properties": {"pod": {"type": "string"}, "namespace": {"type": "string"}, "container": {"type": "string"},
                                                     "previous": {"type": "boolean"}, "tail": {"type": "integer"}}, "required": ["pod"]},
      permissions=["kubernetes.read"])
def kubectl_logs(args: dict[str, Any], ctx: ToolContext) -> Any:
    text = ctx.backend("kubernetes").logs(args["pod"], _ns(args, ctx), args.get("container"), bool(args.get("previous")), int(args.get("tail") or 200))
    return {"pod": args["pod"], "lines": text.splitlines(), "text": text}


@tool("kubectl_events", "List events in a namespace, optionally filtered to one object.", category="kubernetes",
      input_schema={"type": "object", "properties": {"namespace": {"type": "string"}, "involved": {"type": "string"}}},
      permissions=["kubernetes.read"])
def kubectl_events(args: dict[str, Any], ctx: ToolContext) -> Any:
    evs = ctx.backend("kubernetes").events(_ns(args, ctx), args.get("involved"))
    return {"count": len(evs), "events": [{"type": e.get("type"), "reason": e.get("reason"), "message": e.get("message"), "count": e.get("count"),
                                           "object": f"{e.get('involvedObject', {}).get('kind')}/{e.get('involvedObject', {}).get('name')}",
                                           "last": e.get("lastTimestamp")} for e in evs]}


@tool("kubectl_top_pods", "Show CPU/memory usage of pods in a namespace (metrics-server).", category="kubernetes",
      input_schema={"type": "object", "properties": {"namespace": {"type": "string"}}}, permissions=["kubernetes.read"])
def kubectl_top_pods(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"pods": ctx.backend("kubernetes").top_pods(_ns(args, ctx))}


@tool("kubectl_get_nodes", "List cluster nodes with versions, allocatable resources, taints and conditions.", category="kubernetes",
      permissions=["kubernetes.read"])
def kubectl_get_nodes(args: dict[str, Any], ctx: ToolContext) -> Any:
    nodes = ctx.backend("kubernetes").nodes()
    return {"count": len(nodes), "nodes": [{"name": n["metadata"]["name"], "version": n.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"),
                                            "labels": n["metadata"].get("labels", {}), "taints": n.get("spec", {}).get("taints", []),
                                            "allocatable": n.get("status", {}).get("allocatable", {}),
                                            "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", []))}
                                           for n in nodes]}


@tool("kubectl_rollout_history", "Show rollout history of a deployment/statefulset/daemonset.", category="kubernetes",
      input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]},
      permissions=["kubernetes.read"])
def kubectl_rollout_history(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"history": ctx.backend("kubernetes").rollout_history(args.get("kind") or "deployment", args["name"], _ns(args, ctx))}


@tool("kubectl_rollout_status", "Check rollout status of a deployment.", category="kubernetes",
      input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]},
      permissions=["kubernetes.read"])
def kubectl_rollout_status(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"status": ctx.backend("kubernetes").rollout("status", args.get("kind") or "deployment", args["name"], _ns(args, ctx))}


@tool("kubectl_diff", "Server-side diff of a manifest against the live cluster state (read-only).", category="kubernetes",
      permission=PermissionLevel.ANALYZE, input_schema={"type": "object", "properties": {"manifest": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["manifest"]},
      permissions=["kubernetes.read"])
def kubectl_diff(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"diff": ctx.backend("kubernetes").diff(args["manifest"], args.get("namespace"))}


class KubectlApplyTool(Tool):
    spec = ToolSpec(name="kubectl_apply", description="Apply a manifest to the cluster (rolling update for workloads).",
                    risk_level=RiskLevel.HIGH, requires_approval=True, permission=PermissionLevel.DEPLOY, permissions=["kubernetes.write"],
                    timeout=300, rollback="kubectl rollout undo {kind}/{name} -n {namespace}", category="kubernetes", mutating=True,
                    input_schema={"type": "object", "properties": {"manifest": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["manifest"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        be = ctx.backend("kubernetes")
        docs = [d for d in yaml.safe_load_all(args["manifest"]) if d]
        targets = [f"{d.get('kind')}/{d.get('metadata', {}).get('name')}" for d in docs]
        if ctx.dry_run:
            out = be.apply(args["manifest"], args.get("namespace"), dry_run=True)
            return ToolResult(ok=True, output={"dry_run": True, "targets": targets, "output": out}, tool=self.name, args=args, dry_run=True)
        out = be.apply(args["manifest"], args.get("namespace"), dry_run=False)
        return ToolResult(ok=True, output={"targets": targets, "output": out}, tool=self.name, args=args)

    def describe_rollback(self, args: dict[str, Any]) -> Optional[str]:
        docs = [d for d in yaml.safe_load_all(args.get("manifest", "")) if d]
        names = [f"kubectl rollout undo {d.get('kind', '').lower()}/{d.get('metadata', {}).get('name')} -n {d.get('metadata', {}).get('namespace') or args.get('namespace') or 'default'}"
                 for d in docs if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")]
        return "; ".join(names) if names else "re-apply the previous manifest from git history"

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> Optional[ToolResult]:
        be = ctx.backend("kubernetes")
        outs = []
        for d in [d for d in yaml.safe_load_all(args.get("manifest", "")) if d]:
            if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
                ns = d.get("metadata", {}).get("namespace") or args.get("namespace") or "default"
                outs.append(be.rollout("undo", d["kind"].lower(), d["metadata"]["name"], ns))
        return ToolResult(ok=True, output={"rollback": outs}, tool=f"{self.name}.rollback", args=args)


class KubectlRolloutTool(Tool):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(ToolSpec(
            name=f"kubectl_rollout_{action}", description=f"kubectl rollout {action} for a workload.",
            risk_level=RiskLevel.HIGH if action == "restart" else RiskLevel.MEDIUM, requires_approval=True, permission=PermissionLevel.DEPLOY,
            permissions=["kubernetes.write"], timeout=300, category="kubernetes", mutating=True,
            rollback="kubectl rollout undo {kind}/{name} -n {namespace}" if action == "restart" else "kubectl rollout undo again to return to the current revision",
            input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = ctx.backend("kubernetes").rollout(self.action, args.get("kind") or "deployment", args["name"], _ns(args, ctx))
        return ToolResult(ok=True, output={"output": out}, tool=self.name, args=args)

    def rollback(self, args: dict[str, Any], result: ToolResult, ctx: ToolContext) -> Optional[ToolResult]:
        # undoing a restart or an undo means rolling the workload back one more revision
        out = ctx.backend("kubernetes").rollout("undo", args.get("kind") or "deployment", args["name"], _ns(args, ctx))
        return ToolResult(ok=True, output={"output": out}, tool=f"{self.name}.rollback", args=args)


class KubectlDeleteTool(Tool):
    spec = ToolSpec(name="kubectl_delete", description="Delete a Kubernetes resource. DESTRUCTIVE.", risk_level=RiskLevel.CRITICAL,
                    requires_approval=True, permission=PermissionLevel.DESTROY, permissions=["kubernetes.delete"], timeout=180,
                    rollback="re-create the resource from the manifest in git (kubectl apply)", category="kubernetes", mutating=True,
                    input_schema={"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["kind", "name"]})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.dry_run:
            return self.dry_run_result(args, ctx)
        out = ctx.backend("kubernetes").delete(args["kind"], args["name"], _ns(args, ctx))
        return ToolResult(ok=True, output={"output": out}, tool=self.name, args=args)


def build_tools() -> list[Tool]:
    return [kubectl_current_context, kubectl_get, kubectl_describe, kubectl_logs, kubectl_events, kubectl_top_pods, kubectl_get_nodes,
            kubectl_rollout_history, kubectl_rollout_status, kubectl_diff, KubectlApplyTool(), KubectlRolloutTool("restart"),
            KubectlRolloutTool("undo"), KubectlDeleteTool()]
