"""MockWorld: shared in-memory state for every mock backend.

``--mock`` mode and the test-suite build a MockWorld from a named scenario.
Mock backends read from and mutate this object, which lets end-to-end
workflows (diagnose -> plan -> approve -> apply -> validate -> PR -> Jira)
run without any real infrastructure or credentials.

Scenarios
---------
probe-port-mismatch (default) : api pods CrashLoopBackOff, probes on 8000, app on 8080
oom                            : api container OOMKilled (exit 137) with a 512Mi limit
image-pull                     : ImagePullBackOff on a mistyped tag
pending                        : pods Pending, insufficient CPU + taint
config-error                   : CreateContainerConfigError, missing ConfigMap key
healthy                        : everything green
ci-failure                     : GitHub Actions job failed on a lint step
disk-full                      : Linux host with /var at 97 %
"""
from __future__ import annotations

import copy
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional

SCENARIOS = ("probe-port-mismatch", "oom", "image-pull", "pending", "config-error", "healthy", "ci-failure", "disk-full")


@dataclass
class MockWorld:
    scenario: str = "probe-port-mismatch"
    k8s: dict[str, Any] = field(default_factory=dict)
    docker: dict[str, Any] = field(default_factory=dict)
    jira: dict[str, Any] = field(default_factory=dict)
    github: dict[str, Any] = field(default_factory=dict)
    gitlab: dict[str, Any] = field(default_factory=dict)
    linux: dict[str, Any] = field(default_factory=dict)
    aws: dict[str, Any] = field(default_factory=dict)
    terraform: dict[str, Any] = field(default_factory=dict)
    ansible: dict[str, Any] = field(default_factory=dict)
    observability: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    mutations: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def build(cls, scenario: str = "probe-port-mismatch", flags: Optional[dict[str, bool]] = None) -> "MockWorld":
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown mock scenario '{scenario}' (choose from {', '.join(SCENARIOS)})")
        w = cls(scenario=scenario)
        w.flags = {
            "k8s_unreachable": False, "jira_unavailable": False, "aws_creds_expired": False, "git_push_rejected": False,
            "pr_create_fails": False, "terraform_plan_fails": False, "tool_timeout": False, "rollback_fails": False,
            "partial_deploy": False, "docker_unavailable": False, "permission_denied": False,
        }
        w.flags.update(flags or {})
        w._base()
        getattr(w, f"_scenario_{scenario.replace('-', '_')}")()
        return w

    def record(self, kind: str, **data: Any) -> None:
        self.mutations.append({"kind": kind, **data})

    # ------------------------------------------------------------------
    def _base(self) -> None:
        self.k8s = {
            "context": "mock-cluster",
            "namespaces": ["default", "production", "staging", "kube-system"],
            "deployments": {"production": {"api": _deployment("api", image="registry.example.com/sample-app/api:1.4.2",
                                                                 probe_port=8080, container_port=8080, replicas=3, ready=3)}},
            "pods": {"production": [_pod("api-7c98d9b55c-abc12", phase="Running", ready=True),
                                    _pod("api-7c98d9b55c-def34", phase="Running", ready=True),
                                    _pod("api-7c98d9b55c-ghi56", phase="Running", ready=True)]},
            "events": {"production": []},
            "logs": {},
            "services": {"production": {"api": {"metadata": {"name": "api", "namespace": "production"},
                                                "spec": {"selector": {"app": "api"}, "ports": [{"port": 80, "targetPort": 8080}]}}}},
            "endpoints": {"production": {"api": {"metadata": {"name": "api"}, "subsets": [{"addresses": [{"ip": "10.0.1.11"}, {"ip": "10.0.1.12"}, {"ip": "10.0.1.13"}], "ports": [{"port": 8080}]}]}}},
            "ingress": {"production": {"api": {"metadata": {"name": "api"}, "spec": {"rules": [{"host": "api.example.com", "http": {"paths": [{"path": "/", "backend": {"service": {"name": "api", "port": {"number": 80}}}}]}}]}}}},
            "configmaps": {"production": {"api-config": {"metadata": {"name": "api-config"}, "data": {"LOG_LEVEL": "info", "PORT": "8080"}}}},
            "nodes": [_node("ip-10-0-1-10", version="v1.28.5-eks-5e0fdde", cpu_alloc="3920m", mem_alloc="15Gi"),
                      _node("ip-10-0-2-11", version="v1.28.5-eks-5e0fdde", cpu_alloc="3920m", mem_alloc="15Gi"),
                      _node("ip-10-0-3-12", version="v1.27.9-eks-5e0fdde", cpu_alloc="3920m", mem_alloc="15Gi")],
            "top": {"production": [{"name": "api-7c98d9b55c-abc12", "cpu": "120m", "memory": "210Mi"},
                                   {"name": "api-7c98d9b55c-def34", "cpu": "115m", "memory": "205Mi"},
                                   {"name": "api-7c98d9b55c-ghi56", "cpu": "118m", "memory": "208Mi"}]},
            "rollout_history": {"production/api": [{"revision": 6, "change_cause": "image 1.4.1"}, {"revision": 7, "change_cause": "image 1.4.2 + probe change"}]},
            "hpa": {"production": {}},
        }
        self.docker = {
            "containers": [{"Id": "c0ffee01", "Names": "sample-app-api", "Image": "sample-app/api:dev", "State": "running", "Status": "Up 2 hours",
                            "ExitCode": 0, "Ports": "0.0.0.0:8080->8080/tcp", "Mounts": ["/data"], "Memory": "512m"}],
            "images": [{"Repository": "sample-app/api", "Tag": "dev", "Size": "142MB", "Id": "sha256:abc"}],
            "logs": {"sample-app-api": "INFO listening on :8080\nINFO ready\n"},
            "inspect": {"sample-app-api": {"State": {"Status": "running", "ExitCode": 0, "OOMKilled": False, "Error": ""},
                                           "HostConfig": {"Memory": 536870912}, "Config": {"Env": ["PORT=8080"], "ExposedPorts": {"8080/tcp": {}}}}},
            "build_ok": True, "build_log": "Successfully built abc123\n",
        }
        self.jira = {
            "url": "https://jira.example.com",
            "available": True,
            "issues": {
                "DEVOPS-382": {
                    "key": "DEVOPS-382", "summary": "API pods failing readiness checks after port change",
                    "description": textwrap.dedent("""\
                        After the 1.4.2 release the api deployment in the production namespace never becomes Ready.
                        Pods restart continuously. The application was changed to listen on port 8080 in 1.4.2
                        (see PR #398) but the Kubernetes manifests were not updated.

                        Repository: examples/sample-app
                        Service: api
                        Namespace: production
                        Cluster: mock-cluster

                        Acceptance criteria:
                        - Liveness and readiness probes target the port the application listens on
                        - Manifest tests pass
                        - A pull request is opened with the fix
                        """),
                    "status": "To Do", "priority": "High", "labels": ["kubernetes", "production", "incident"],
                    "components": ["api"], "assignee": None, "reporter": "sre-oncall", "issuetype": "Bug",
                    "acceptance_criteria": ["probes target the application port", "manifest tests pass", "PR opened"],
                    "links": [{"type": "relates to", "key": "DEVOPS-380"}], "epic": "DEVOPS-300", "sprint": "Sprint 42",
                    "attachments": [], "worklogs": [], "comments": [{"author": "sre-oncall", "body": "Rolled back to 1.4.1 temporarily."}],
                    "custom_repository": "examples/sample-app",
                },
                "DEVOPS-380": {"key": "DEVOPS-380", "summary": "Move api service to port 8080", "status": "Done", "priority": "Medium",
                                "description": "Application now listens on 8080. Manifests must be updated separately.", "labels": [],
                                "components": ["api"], "assignee": "dev1", "reporter": "dev1", "issuetype": "Task", "comments": [],
                                "acceptance_criteria": [], "links": [], "epic": None, "sprint": None, "attachments": [], "worklogs": []},
            },
            "transitions": {"To Do": ["In Progress"], "In Progress": ["In Review", "To Do"], "In Review": ["Done", "In Progress"], "Done": []},
        }
        self.github = {
            "repo": "example-org/sample-app", "default_branch": "main", "prs": [], "next_pr": 421, "comments": {},
            "workflow_runs": [{"id": 993, "name": "ci", "head_branch": "main", "status": "completed", "conclusion": "success",
                               "head_sha": "9f1c2ab", "created_at": "2026-09-03T10:12:00Z", "html_url": "https://github.com/example-org/sample-app/actions/runs/993"}],
            "jobs": {993: [{"id": 5001, "name": "test", "conclusion": "success", "steps": [{"name": "pytest", "conclusion": "success"}]}]},
            "job_logs": {5001: "collected 3 items\n3 passed in 0.42s\n"},
            "commits": [{"sha": "9f1c2ab", "message": "release 1.4.2: listen on 8080 (DEVOPS-380)", "author": "dev1", "date": "2026-09-03T10:00:00Z", "files": ["app/server.py", "k8s/deployment.yaml"]},
                        {"sha": "71e0d4c", "message": "chore: bump base image", "author": "dev2", "date": "2026-09-01T09:00:00Z", "files": ["Dockerfile"]}],
            "protected_branches": ["main"],
        }
        self.gitlab = {"project": "example-org/sample-app", "mrs": [], "next_mr": 77, "pipelines": [
            {"id": 1200, "status": "success", "ref": "main", "sha": "9f1c2ab", "web_url": "https://gitlab.example.com/example-org/sample-app/-/pipelines/1200"}],
            "jobs": {1200: [{"id": 3300, "name": "test", "status": "success", "stage": "test"}]}, "job_logs": {3300: "3 passed\n"}}
        self.linux = {
            "hostname": "api-host-01",
            "commands": {
                "uptime": " 10:42:01 up 12 days,  3:11,  1 user,  load average: 0.42, 0.51, 0.47\n",
                "df -h": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme0n1p1   80G   41G   39G  52% /\n/dev/nvme1n1    200G   96G  104G  48% /var\n",
                "free -m": "              total        used        free      shared  buff/cache   available\nMem:          15882        6210        4120         120        5552        9210\nSwap:             0           0           0\n",
                "systemctl status api": "● api.service - Sample API\n     Loaded: loaded (/etc/systemd/system/api.service; enabled)\n     Active: active (running) since Tue 2026-09-01 09:00:12 UTC; 3 days ago\n   Main PID: 1234 (python)\n",
                "systemctl --failed": "0 loaded units listed.\n",
                "journalctl -u api -n 50 --no-pager": "Sep 04 10:40:01 api-host-01 python[1234]: INFO listening on :8080\n",
                "ps aux --sort=-%mem | head -n 10": "USER PID %CPU %MEM COMMAND\napp 1234 1.2 4.1 python server.py\n",
                "ss -tulpn": "Netid State  Local Address:Port Process\ntcp   LISTEN 0.0.0.0:8080 users:((\"python\",pid=1234,fd=5))\ntcp   LISTEN 0.0.0.0:22 users:((\"sshd\",pid=800,fd=3))\n",
                "ip addr": "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9001\n    inet 10.0.1.10/24 scope global eth0\n",
                "ip route": "default via 10.0.1.1 dev eth0\n10.0.1.0/24 dev eth0 proto kernel scope link src 10.0.1.10\n",
                "dmesg -T | tail -n 50": "[Thu Sep  4 10:00:00 2026] eth0: link up\n",
                "cat /etc/os-release": "NAME=\"Ubuntu\"\nVERSION_ID=\"22.04\"\n",
                "du -sh /var/* | sort -rh | head -n 10": "60G\t/var/lib\n30G\t/var/log\n4G\t/var/cache\n",
            },
        }
        self.aws = {
            "identity": {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/devops-agent-readonly/session", "UserId": "AROAMOCK"},
            "responses": {
                ("eks", "describe-cluster"): {"cluster": {"name": "mock-cluster", "version": "1.28", "status": "ACTIVE", "endpoint": "https://mock.eks.amazonaws.com",
                                                          "resourcesVpcConfig": {"vpcId": "vpc-0abc", "subnetIds": ["subnet-1", "subnet-2", "subnet-3"]}}},
                ("eks", "list-nodegroups"): {"nodegroups": ["workers-a", "workers-b"]},
                ("eks", "describe-nodegroup"): {"nodegroup": {"nodegroupName": "workers-a", "version": "1.27", "releaseVersion": "1.27.9-20240117", "status": "ACTIVE",
                                                              "scalingConfig": {"minSize": 2, "maxSize": 6, "desiredSize": 3}, "instanceTypes": ["m6i.large"],
                                                              "amiType": "AL2_x86_64", "updateConfig": {"maxUnavailable": 1}}},
                ("ec2", "describe-instances"): {"Reservations": [{"Instances": [{"InstanceId": "i-0a1", "InstanceType": "m6i.large", "State": {"Name": "running"}, "PrivateIpAddress": "10.0.1.10"},
                                                                                {"InstanceId": "i-0a2", "InstanceType": "m6i.large", "State": {"Name": "running"}, "PrivateIpAddress": "10.0.2.11"},
                                                                                {"InstanceId": "i-0a3", "InstanceType": "m6i.large", "State": {"Name": "running"}, "PrivateIpAddress": "10.0.3.12"}]}]},
                ("elbv2", "describe-target-health"): {"TargetHealthDescriptions": [{"Target": {"Id": "10.0.1.11", "Port": 8080}, "TargetHealth": {"State": "healthy"}}]},
                ("logs", "filter-log-events"): {"events": [{"timestamp": 1757000000000, "message": "INFO listening on :8080"}]},
                ("cloudwatch", "get-metric-statistics"): {"Datapoints": [{"Timestamp": "2026-09-04T10:00:00Z", "Average": 0.4, "Unit": "Percent"}]},
                ("rds", "describe-db-instances"): {"DBInstances": [{"DBInstanceIdentifier": "app-db", "DBInstanceStatus": "available", "Engine": "postgres", "DBInstanceClass": "db.t3.medium"}]},
                ("s3api", "list-buckets"): {"Buckets": [{"Name": "example-artifacts"}]},
                ("iam", "list-attached-role-policies"): {"AttachedPolicies": [{"PolicyName": "ReadOnlyAccess"}]},
            },
        }
        self.terraform = {
            "validate_ok": True, "fmt_ok": True,
            "plan_output": textwrap.dedent("""\
                Terraform will perform the following actions:

                  # module.eks.aws_eks_node_group.workers_a will be updated in-place
                  ~ resource "aws_eks_node_group" "workers_a" {
                      ~ version         = "1.27" -> "1.28"
                      ~ release_version = "1.27.9-20240117" -> "1.28.5-20240202"
                    }

                Plan: 0 to add, 1 to change, 0 to destroy.
                """),
            "plan_summary": {"add": 0, "change": 1, "destroy": 0},
        }
        self.ansible = {"check_output": "PLAY [web] ***\nTASK [Gathering Facts] ***\nok: [api-host-01]\nTASK [restart api] ***\nchanged: [api-host-01]\nPLAY RECAP ***\napi-host-01 : ok=2 changed=1 unreachable=0 failed=0\n",
                        "run_output": "PLAY RECAP ***\napi-host-01 : ok=2 changed=1 unreachable=0 failed=0\n"}
        self.observability = {
            "prometheus": {
                'sum(rate(http_requests_total{job="api",code=~"5.."}[5m])) / sum(rate(http_requests_total{job="api"}[5m]))': 0.002,
                'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="api"}[5m])) by (le))': 0.18,
                'kube_deployment_status_replicas_available{deployment="api",namespace="production"}': 3,
                'sum(kube_pod_container_status_restarts_total{namespace="production",container="api"})': 0,
                'container_memory_working_set_bytes{container="api",namespace="production"}': 220 * 1024 * 1024,
                'up{job="api"}': 1,
            },
            "alerts": [],
            "deployments": [{"time": "2026-09-01T09:05:00Z", "service": "api", "version": "1.4.1", "by": "argocd", "namespace": "production"}],
            "loki": {'{namespace="production",app="api"}': ["INFO listening on :8080", "INFO ready"]},
        }
        self.security = {"trivy": {"image": {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 4}, "fs": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 1}},
                         "gitleaks": [], "semgrep": [], "checkov": {"passed": 12, "failed": 1, "findings": ["CKV_K8S_8: Liveness probe should be configured"]}}
        self.network = {"dns": {"api.example.com": ["203.0.113.10"], "jira.example.com": ["203.0.113.20"], "db.internal": ["10.0.5.20"]},
                        "tcp": {("api.example.com", 443): True, ("db.internal", 5432): True, ("10.0.5.20", 5432): True},
                        "http": {"https://api.example.com/healthz": (200, "ok")}}

    # ------------------------------------------------------------------
    def _broken_api(self, *, probe_port: int, container_port: int, image: str = "registry.example.com/sample-app/api:1.4.2") -> None:
        self.k8s["deployments"]["production"]["api"] = _deployment("api", image=image, probe_port=probe_port, container_port=container_port,
                                                                    replicas=3, ready=0)
        self.k8s["endpoints"]["production"]["api"] = {"metadata": {"name": "api"}, "subsets": []}
        self.observability["prometheus"]['kube_deployment_status_replicas_available{deployment="api",namespace="production"}'] = 0
        self.observability["prometheus"]['sum(rate(http_requests_total{job="api",code=~"5.."}[5m])) / sum(rate(http_requests_total{job="api"}[5m]))'] = 0.98
        self.observability["prometheus"]['up{job="api"}'] = 0
        self.observability["alerts"] = [{"name": "APIHighErrorRate", "severity": "critical", "since": "2026-09-04T10:22:00Z", "summary": "5xx ratio > 50% for 5m"},
                                        {"name": "KubeDeploymentReplicasMismatch", "severity": "warning", "since": "2026-09-04T10:19:00Z", "summary": "api has 0/3 available"}]
        self.observability["deployments"].append({"time": "2026-09-04T10:17:00Z", "service": "api", "version": "1.4.2", "by": "argocd", "namespace": "production",
                                                  "commit": "9f1c2ab"})
        self.network["http"]["https://api.example.com/healthz"] = (503, "Service Unavailable")

    def _scenario_probe_port_mismatch(self) -> None:
        self._broken_api(probe_port=8000, container_port=8080)
        pods = []
        for i, suffix in enumerate(("abc12", "def34", "ghi56")):
            pods.append(_pod(f"api-7c98d9b55c-{suffix}", phase="Running", ready=False, restarts=12 + i, waiting_reason="CrashLoopBackOff",
                             last_exit=137, last_reason="Error"))
        self.k8s["pods"]["production"] = pods
        self.k8s["events"]["production"] = [
            _event("api-7c98d9b55c-abc12", "Warning", "Unhealthy", "Readiness probe failed: dial tcp 10.0.1.21:8000: connect: connection refused", 41),
            _event("api-7c98d9b55c-abc12", "Warning", "Unhealthy", "Liveness probe failed: dial tcp 10.0.1.21:8000: connect: connection refused", 13),
            _event("api-7c98d9b55c-abc12", "Normal", "Killing", "Container api failed liveness probe, will be restarted", 13),
            _event("api-7c98d9b55c-abc12", "Warning", "BackOff", "Back-off restarting failed container api in pod api-7c98d9b55c-abc12_production", 40),
        ]
        for p in pods:
            self.k8s["logs"][("production", p["metadata"]["name"])] = "INFO starting sample-app api 1.4.2\nINFO listening on :8080\nINFO ready\nINFO received SIGTERM, shutting down\n"
        self.k8s["top"]["production"] = [{"name": p["metadata"]["name"], "cpu": "15m", "memory": "48Mi"} for p in pods]
        self.observability["prometheus"]['sum(kube_pod_container_status_restarts_total{namespace="production",container="api"})'] = 38
        self.observability["loki"]['{namespace="production",app="api"}'] = ["INFO listening on :8080", "INFO ready", "INFO received SIGTERM, shutting down"]

    def _scenario_oom(self) -> None:
        self._broken_api(probe_port=8080, container_port=8080)
        dep = self.k8s["deployments"]["production"]["api"]
        dep["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"memory": "512Mi", "cpu": "500m"}, "requests": {"memory": "256Mi", "cpu": "100m"}}
        pods = [_pod(f"api-5d4f7b9c8-{s}", phase="Running", ready=False, restarts=7, waiting_reason="CrashLoopBackOff", last_exit=137, last_reason="OOMKilled")
                for s in ("aaa11", "bbb22", "ccc33")]
        self.k8s["pods"]["production"] = pods
        self.k8s["events"]["production"] = [_event("api-5d4f7b9c8-aaa11", "Warning", "BackOff", "Back-off restarting failed container api", 20)]
        for p in pods:
            self.k8s["logs"][("production", p["metadata"]["name"])] = "INFO listening on :8080\nINFO loading catalog into memory (2.1M rows)\n"
        self.k8s["top"]["production"] = [{"name": p["metadata"]["name"], "cpu": "480m", "memory": "510Mi"} for p in pods]
        self.observability["prometheus"]['container_memory_working_set_bytes{container="api",namespace="production"}'] = 535 * 1024 * 1024

    def _scenario_image_pull(self) -> None:
        self._broken_api(probe_port=8080, container_port=8080, image="registry.example.com/sample-app/api:1.4.2-rc")
        pods = [_pod(f"api-66c9d8f7b-{s}", phase="Pending", ready=False, restarts=0, waiting_reason="ImagePullBackOff") for s in ("x1", "x2", "x3")]
        self.k8s["pods"]["production"] = pods
        self.k8s["events"]["production"] = [
            _event("api-66c9d8f7b-x1", "Warning", "Failed", 'Failed to pull image "registry.example.com/sample-app/api:1.4.2-rc": manifest unknown: manifest unknown', 9),
            _event("api-66c9d8f7b-x1", "Warning", "Failed", "Error: ErrImagePull", 9),
            _event("api-66c9d8f7b-x1", "Normal", "BackOff", 'Back-off pulling image "registry.example.com/sample-app/api:1.4.2-rc"', 30),
        ]

    def _scenario_pending(self) -> None:
        self._broken_api(probe_port=8080, container_port=8080)
        dep = self.k8s["deployments"]["production"]["api"]
        dep["spec"]["template"]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "4", "memory": "8Gi"}}
        pods = [_pod(f"api-77d8c9b6a-{s}", phase="Pending", ready=False, restarts=0) for s in ("p1", "p2", "p3")]
        self.k8s["pods"]["production"] = pods
        self.k8s["events"]["production"] = [
            _event("api-77d8c9b6a-p1", "Warning", "FailedScheduling", "0/3 nodes are available: 1 node(s) had untolerated taint {dedicated: gpu}, 2 Insufficient cpu. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod.", 15),
        ]
        self.k8s["nodes"][2]["spec"] = {"taints": [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]}

    def _scenario_config_error(self) -> None:
        self._broken_api(probe_port=8080, container_port=8080)
        dep = self.k8s["deployments"]["production"]["api"]
        dep["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "DB_URL", "valueFrom": {"configMapKeyRef": {"name": "api-config", "key": "DB_URL"}}}]
        pods = [_pod(f"api-8f6d5c4b3-{s}", phase="Pending", ready=False, restarts=0, waiting_reason="CreateContainerConfigError") for s in ("c1", "c2", "c3")]
        self.k8s["pods"]["production"] = pods
        self.k8s["events"]["production"] = [
            _event("api-8f6d5c4b3-c1", "Warning", "Failed", 'Error: couldn\'t find key DB_URL in ConfigMap production/api-config', 22),
        ]

    def _scenario_healthy(self) -> None:
        pass

    def _scenario_ci_failure(self) -> None:
        self.github["workflow_runs"].insert(0, {"id": 994, "name": "ci", "head_branch": "feature/DEVOPS-390-lint", "status": "completed", "conclusion": "failure",
                                                "head_sha": "c0ffee1", "created_at": "2026-09-04T09:30:00Z",
                                                "html_url": "https://github.com/example-org/sample-app/actions/runs/994"})
        self.github["jobs"][994] = [{"id": 5002, "name": "lint", "conclusion": "failure",
                                     "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": "Run ruff", "conclusion": "failure"}, {"name": "Run pytest", "conclusion": "skipped"}]}]
        self.github["job_logs"][5002] = ("##[group]Run ruff\napp/server.py:12:1: F401 'os' imported but unused\napp/server.py:40:80: E501 line too long (94 > 88)\n"
                                         "Found 2 errors.\n##[error]Process completed with exit code 1.\n")
        self.gitlab["pipelines"].insert(0, {"id": 1201, "status": "failed", "ref": "feature/DEVOPS-390-lint", "sha": "c0ffee1",
                                            "web_url": "https://gitlab.example.com/example-org/sample-app/-/pipelines/1201"})
        self.gitlab["jobs"][1201] = [{"id": 3301, "name": "lint", "status": "failed", "stage": "test"}]
        self.gitlab["job_logs"][3301] = self.github["job_logs"][5002]

    def _scenario_disk_full(self) -> None:
        self.linux["commands"]["df -h"] = "Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme0n1p1   80G   41G   39G  52% /\n/dev/nvme1n1    200G  194G  6.0G  97% /var\n"
        self.linux["commands"]["du -sh /var/* | sort -rh | head -n 10"] = "150G\t/var/log\n40G\t/var/lib\n4G\t/var/cache\n"
        self.linux["commands"]["journalctl -u api -n 50 --no-pager"] = "Sep 04 10:40:01 api-host-01 python[1234]: ERROR OSError: [Errno 28] No space left on device\n"
        self.linux["commands"]["systemctl status api"] = "● api.service - Sample API\n     Active: failed (Result: exit-code) since Thu 2026-09-04 10:40:01 UTC\n"
        self.linux["commands"]["systemctl --failed"] = "  UNIT        LOAD   ACTIVE SUB    DESCRIPTION\n● api.service loaded failed failed Sample API\n1 loaded units listed.\n"
        self.linux["commands"]["ls -lS /var/log | head -n 10"] = "-rw-r----- 1 syslog adm 148G Sep  4 10:40 syslog\n-rw-r----- 1 syslog adm 1.2G Sep  1 00:00 syslog.1\n"

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({"scenario": self.scenario, "flags": self.flags, "mutations": self.mutations})


# ----------------------------------------------------------------------
# builders
# ----------------------------------------------------------------------
def _deployment(name: str, *, image: str, probe_port: int, container_port: int, replicas: int, ready: int) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": "production", "labels": {"app": name}, "generation": 7,
                     "annotations": {"deployment.kubernetes.io/revision": "7"}},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                 "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"containers": [{"name": name, "image": image, "ports": [{"containerPort": container_port, "name": "http"}],
                                                       "readinessProbe": {"httpGet": {"path": "/healthz", "port": probe_port}, "initialDelaySeconds": 5, "periodSeconds": 10, "failureThreshold": 3},
                                                       "livenessProbe": {"httpGet": {"path": "/healthz", "port": probe_port}, "initialDelaySeconds": 15, "periodSeconds": 20, "failureThreshold": 3},
                                                       "resources": {"limits": {"memory": "512Mi", "cpu": "500m"}, "requests": {"memory": "128Mi", "cpu": "100m"}},
                                                       "env": [{"name": "PORT", "value": str(container_port)}]}],
                                       "serviceAccountName": "api"}}},
        "status": {"replicas": replicas, "readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": replicas, "unavailableReplicas": replicas - ready,
                   "observedGeneration": 7,
                   "conditions": [{"type": "Available", "status": "True" if ready == replicas else "False",
                                   "reason": "MinimumReplicasAvailable" if ready == replicas else "MinimumReplicasUnavailable"},
                                  {"type": "Progressing", "status": "True" if ready == replicas else "False",
                                   "reason": "NewReplicaSetAvailable" if ready == replicas else "ProgressDeadlineExceeded"}]},
    }


def _pod(name: str, *, phase: str, ready: bool, restarts: int = 0, waiting_reason: Optional[str] = None, last_exit: Optional[int] = None,
         last_reason: Optional[str] = None, node: str = "ip-10-0-1-10") -> dict[str, Any]:
    state: dict[str, Any] = {"running": {"startedAt": "2026-09-04T10:41:00Z"}} if ready or not waiting_reason else {"waiting": {"reason": waiting_reason, "message": ""}}
    cs: dict[str, Any] = {"name": "api", "ready": ready, "restartCount": restarts, "state": state, "image": "registry.example.com/sample-app/api:1.4.2"}
    if last_exit is not None:
        cs["lastState"] = {"terminated": {"exitCode": last_exit, "reason": last_reason or "Error", "finishedAt": "2026-09-04T10:40:30Z"}}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "production", "labels": {"app": "api", "pod-template-hash": name.split("-")[1]},
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "-".join(name.split("-")[:2])}], "creationTimestamp": "2026-09-04T10:17:30Z"},
        "spec": {"nodeName": node if phase != "Pending" or waiting_reason else None, "containers": [{"name": "api", "image": cs["image"]}]},
        "status": {"phase": phase, "podIP": "10.0.1.21", "containerStatuses": [cs],
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False", "reason": None if ready else "ContainersNotReady"}]},
    }


def _event(pod: str, etype: str, reason: str, message: str, count: int) -> dict[str, Any]:
    return {"type": etype, "reason": reason, "message": message, "count": count, "involvedObject": {"kind": "Pod", "name": pod, "namespace": "production"},
            "lastTimestamp": "2026-09-04T10:41:12Z", "source": {"component": "kubelet"}}


def _node(name: str, *, version: str, cpu_alloc: str, mem_alloc: str) -> dict[str, Any]:
    return {"metadata": {"name": name, "labels": {"eks.amazonaws.com/nodegroup": "workers-a", "node.kubernetes.io/instance-type": "m6i.large"}},
            "status": {"nodeInfo": {"kubeletVersion": version, "osImage": "Amazon Linux 2", "containerRuntimeVersion": "containerd://1.7.11"},
                       "allocatable": {"cpu": cpu_alloc, "memory": mem_alloc, "pods": "29"},
                       "conditions": [{"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "False"}, {"type": "DiskPressure", "status": "False"}]},
            "spec": {}}
