from agent.models import TaskKind, TaskStatus


def test_docker_specialist_reports_no_fault_on_healthy_container(make_harness):
    t = make_harness().run("why is my docker container sample-app-api misbehaving?")
    assert t.status == TaskStatus.COMPLETED and "docker-agent" in t.specialists
    assert t.diagnosis.conclusion and "No fault detected" in t.diagnosis.conclusion


def test_linux_specialist_confirms_disk_full(make_harness):
    t = make_harness(scenario="disk-full").run("the api service on host api-host-01 is failing, disk space?")
    assert "linux-agent" in t.specialists
    assert t.diagnosis.conclusion and "/var is 97% full" in t.diagnosis.conclusion
    assert any("No space left" in f.statement for f in t.facts())
    assert t.plan and any("Reclaim space" in c.description for c in t.plan.changes)


def test_cicd_specialist_finds_lint_failure(make_harness):
    t = make_harness(scenario="ci-failure").run("why did the github actions pipeline fail on run 994?")
    assert "cicd-agent" in t.specialists
    assert t.diagnosis.conclusion and "lint" in t.diagnosis.conclusion and "F401" in t.diagnosis.conclusion
    assert t.links.pipeline and "994" in t.links.pipeline


def test_networking_specialist_explains_503_via_backend(make_harness):
    t = make_harness().run("https://api.example.com/healthz returns 503, dns or network issue?")
    assert "networking-agent" in t.specialists
    assert any("HTTP GET https://api.example.com/healthz -> 503" in f.statement for f in t.facts())


def test_terraform_specialist_analyses_plan_risk(make_harness, sample_repo):
    (sample_repo / "main.tf").write_text('resource "aws_eks_node_group" "workers_a" {}\n')
    h = make_harness()
    t = h.run("review the terraform plan for the eks module", repo=sample_repo)
    assert "terraform-agent" in t.specialists
    assert any("1 to change" in f.statement for f in t.facts())
    assert t.diagnosis.conclusion and "Plan changes infrastructure" in t.diagnosis.conclusion


def test_terraform_plan_failure_is_reported_not_hidden(make_harness, sample_repo):
    (sample_repo / "main.tf").write_text("terraform {}\n")
    t = make_harness(flags={"terraform_plan_fails": True}).run("terraform plan for this module", repo=sample_repo)
    assert any("terraform plan failed" in f.statement for f in t.facts())
    assert t.diagnosis.conclusion and "terraform plan fails" in t.diagnosis.conclusion


def test_ansible_specialist_runs_check_mode_first(make_harness, sample_repo):
    (sample_repo / "site.yml").write_text("- hosts: web\n  tasks: []\n")
    (sample_repo / "inventory").write_text("[web]\napi-host-01\n")
    t = make_harness().run("run the ansible playbook site.yml on the web hosts", repo=sample_repo)
    assert "ansible-agent" in t.specialists
    assert any("Check mode for site.yml" in f.statement for f in t.facts())
    names = [c.tool for c in t.tool_calls]
    assert "ansible_check" in names and "ansible_run" not in names  # read-only/diagnose never runs the playbook


def test_aws_specialist_verifies_identity_and_version_skew(make_harness):
    t = make_harness().run("check the eks nodegroup versions in aws")
    assert "aws-agent" in t.specialists
    assert any("account 123456789012" in f.statement for f in t.facts())
    assert t.diagnosis.conclusion and "version skew" in t.diagnosis.conclusion


def test_security_specialist_scans_workspace(make_harness, sample_repo):
    (sample_repo / "bad.env").write_text("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYQQQQQQQQ\n")
    t = make_harness().run("run a security scan for secrets in this repository", repo=sample_repo)
    assert "security-agent" in t.specialists
    assert t.diagnosis.conclusion and "credential-like" in t.diagnosis.conclusion
    assert "wJalrXUtnFEMI" not in " ".join(f.statement for f in t.facts())


def test_git_specialist_reviews_pull_request(make_harness):
    h = make_harness()
    h.world.github["branches"] = ["feature/x"]
    h.backends["github"].create_pr("example-org/sample-app", "feature/x", "main", "probe fix", "body")
    t = h.run("review PR #421")
    assert "git-agent" in t.specialists and t.links.pull_request and "421" in t.links.pull_request
    assert any("PR review completed" in h_.statement for h_ in t.diagnosis.hypotheses)


def test_observability_specialist_correlates_deployment(make_harness):
    t = make_harness().run("error rate for the api service is high, check prometheus alerts")
    assert "observability-agent" in t.specialists
    assert any("precedes the first alert" in e.statement for e in t.evidence)


def test_plan_kind_never_mutates_and_uses_runbook(make_harness):
    h = make_harness()
    t = h.run("upgrade our Kubernetes worker nodes", kind=TaskKind.PLAN)
    assert t.status == TaskStatus.COMPLETED and t.plan and t.plan.steps
    assert any("kubernetes-worker-node-upgrade" in i for i in t.plan.infrastructure)
    assert any("version skew" in (t.diagnosis.conclusion or "") for _ in [0])
    assert t.plan.cost_notes and t.plan.rollback and t.plan.validation
    assert h.world.mutations == []
    assert h.store.read_artifact(t.id, "plan.md") and (h.config.agent_dir / "plan.md").exists()
