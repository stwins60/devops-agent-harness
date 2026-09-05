"""Manifest tests: probes must target a declared containerPort and the service targetPort must match."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return [d for d in yaml.safe_load_all((ROOT / "k8s" / name).read_text(encoding="utf-8")) if d]


def test_probe_ports_match_container_ports():
    (dep,) = _load("deployment.yaml")
    for c in dep["spec"]["template"]["spec"]["containers"]:
        ports = {p["containerPort"] for p in c.get("ports", [])} | {p.get("name") for p in c.get("ports", [])}
        for probe in ("readinessProbe", "livenessProbe"):
            port = c[probe]["httpGet"]["port"]
            assert port in ports, f"{probe} targets port {port}, containerPorts are {sorted(p for p in ports if isinstance(p, int))}"


def test_service_target_port_matches_container_port():
    (dep,) = _load("deployment.yaml")
    (svc,) = _load("service.yaml")
    container_ports = {p["containerPort"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]}
    for p in svc["spec"]["ports"]:
        assert p["targetPort"] in container_ports


def test_deployment_has_resource_limits():
    (dep,) = _load("deployment.yaml")
    for c in dep["spec"]["template"]["spec"]["containers"]:
        assert c["resources"]["limits"]["memory"]
