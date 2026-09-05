"""AWS specialist: read-only diagnosis through describe/list/get APIs; mutations only via approved plans."""
from __future__ import annotations

from typing import Optional

from agent.models import Diagnosis, Hypothesis, Plan
from agent.rca.engine import EvidenceLog
from agent.specialists.base import Investigation, Specialist


class AwsSpecialist(Specialist):
    name = "aws-agent"
    description = "Inspects AWS resources (EKS, EC2, ELB, RDS, IAM, CloudWatch) using read-only APIs and verifies account identity."
    domains = ["aws"]
    keywords = ["aws", "ec2", "ecs", "eks", "s3", "iam", "rds", "lambda", "cloudwatch", "alb", "nlb", "route53", "vpc", "ecr", "nodegroup", "node group", "account"]

    def investigate(self, inv: Investigation) -> None:
        ident = self.call(inv, "aws_identity", {}, purpose="verify AWS account identity")
        if not ident.ok:
            if ident.failure_kind in ("auth", "permission", "network", "unavailable"):
                inv.blocked = f"AWS access unavailable: {ident.error}"
            return
        account, arn = ident.output.get("Account"), ident.output.get("Arn")
        bound = self.h.config.environment_for(aws_account=account)
        inv.log.fact(f"AWS identity: account {account}, principal {arn}{' (bound to environment ' + bound.value + ')' if bound else ' (account not bound to any environment in config)'}.",
                     source="aws_identity", aws_account=account)
        if bound and bound.strictness > inv.task.environment.strictness:
            inv.task.environment = bound
            inv.log.fact(f"Environment escalated to '{bound.value}' because the AWS account is bound to it.", source="environment-resolver")
        cluster = inv.target("cluster") or self.h.config.extra.get("eks_cluster") or ("mock-cluster" if self.h.config.mock else None)
        low = inv.task.request.lower()
        if cluster and any(w in low for w in ("eks", "node", "kubernetes", "cluster", "upgrade", "worker")):
            c = self.call(inv, "aws_describe", {"service": "eks", "operation": "describe-cluster", "params": {"name": cluster}}, purpose="EKS cluster details")
            if c.ok and c.output.get("cluster"):
                cl = c.output["cluster"]
                inv.log.fact(f"EKS cluster {cl.get('name')}: version {cl.get('version')}, status {cl.get('status')}.", source="aws_describe(eks describe-cluster)",
                             eks_version=cl.get("version"), eks_cluster=cl.get("name"))
            ngs = self.call(inv, "aws_describe", {"service": "eks", "operation": "list-nodegroups", "params": {"cluster-name": cluster}}, purpose="node groups")
            for ng in (ngs.output.get("nodegroups", []) if ngs.ok else [])[:3]:
                d = self.call(inv, "aws_describe", {"service": "eks", "operation": "describe-nodegroup", "params": {"cluster-name": cluster, "nodegroup-name": ng}}, purpose=f"node group {ng}")
                if d.ok and d.output.get("nodegroup"):
                    n = d.output["nodegroup"]
                    inv.log.fact(f"Node group {n.get('nodegroupName')}: version {n.get('version')} ({n.get('releaseVersion')}), {n.get('scalingConfig')}, "
                                 f"instance types {n.get('instanceTypes')}, maxUnavailable {n.get('updateConfig', {}).get('maxUnavailable')}.",
                                 source=f"aws_describe(eks describe-nodegroup {ng})", nodegroup=n.get("nodegroupName"), nodegroup_version=n.get("version"),
                                 nodegroup_desired=(n.get("scalingConfig") or {}).get("desiredSize"), instance_types=n.get("instanceTypes"))
        if any(w in low for w in ("ec2", "instance", "node", "host")):
            e = self.call(inv, "aws_describe", {"service": "ec2", "operation": "describe-instances", "params": {}}, purpose="EC2 instances")
            if e.ok:
                inst = [i for r in e.output.get("Reservations", []) for i in r.get("Instances", [])]
                inv.log.fact(f"{len(inst)} EC2 instance(s): " + ", ".join(f"{i.get('InstanceId')}({i.get('InstanceType')},{i.get('State', {}).get('Name')})" for i in inst[:6]),
                             source="aws_describe(ec2 describe-instances)", ec2_count=len(inst))
        if any(w in low for w in ("alb", "load balancer", "target", "503", "502")):
            th = self.call(inv, "aws_describe", {"service": "elbv2", "operation": "describe-target-health", "params": {}}, purpose="target health")
            if th.ok:
                states = [t.get("TargetHealth", {}).get("State") for t in th.output.get("TargetHealthDescriptions", [])]
                inv.log.fact(f"Load balancer target health: {states}.", source="aws_describe(elbv2 describe-target-health)", target_states=states)
        if any(w in low for w in ("rds", "database", "db")):
            r = self.call(inv, "aws_describe", {"service": "rds", "operation": "describe-db-instances", "params": {}}, purpose="RDS instances")
            if r.ok:
                dbs = r.output.get("DBInstances", [])
                inv.log.fact("RDS: " + ", ".join(f"{d.get('DBInstanceIdentifier')}({d.get('Engine')},{d.get('DBInstanceStatus')})" for d in dbs), source="aws_describe(rds)")

    def analyzers(self):
        return [("aws.version_drift", _version_drift), ("aws.unhealthy_targets", _unhealthy_targets)]

    def propose(self, inv: Investigation, diagnosis: Diagnosis) -> Optional[Plan]:
        return None


def _version_drift(log: EvidenceLog) -> list[Hypothesis]:
    cluster_v, ng_v = log.get("eks_version"), log.get("nodegroup_version")
    if cluster_v and ng_v and cluster_v != ng_v:
        log.recommendation(f"Upgrade node group {log.get('nodegroup')} from {ng_v} to {cluster_v} (control plane version) using a rolling update.")
        return [Hypothesis(statement=f"Node group {log.get('nodegroup')} runs Kubernetes {ng_v} while the control plane is {cluster_v} (version skew).",
                           validation="describe-cluster vs describe-nodegroup versions.", status="confirmed", confidence=0.95)]
    return []


def _unhealthy_targets(log: EvidenceLog) -> list[Hypothesis]:
    states = log.get("target_states") or []
    if states and all(s != "healthy" for s in states):
        return [Hypothesis(statement=f"All load balancer targets are unhealthy ({states}); the ALB returns 503.", validation="describe-target-health.", status="confirmed", confidence=0.9)]
    return []
