# Terraform agent

## Workflow

```text
inspect configuration -> terraform fmt -check -> terraform validate -> terraform plan -> risk analysis -> approval -> terraform apply -> verification (plan shows no changes)
```

* `terraform_plan` runs with `-input=false -detailed-exitcode -lock=false` and analyses the output:
  adds / changes / destroys / replacements, resource addresses, sensitive resource types
  (IAM, security groups, RDS/DB, KMS, Route53, network ACLs) and a risk level
  (low / medium / high / critical).
* `terraform_apply` re-plans first and refuses when the plan destroys resources unless
  `allow_destroy=true` was set after explicit human approval. Rollback: restore the previous
  configuration from git, plan, apply.
* `terraform_destroy` is DESTROY: never automatic, explicit confirmation always, rollback stated
  as NOT AVAILABLE.
* `terraform fmt` (writing) is classified CAUTION because it rewrites files; `fmt -check` is SAFE.
* State mutation commands (`state rm/mv/push`) are DANGEROUS.

## Mock behaviour

`MockTerraformBackend` returns a node-group version bump plan (`0 add / 1 change / 0 destroy`);
`--flag terraform_plan_fails` simulates missing provider credentials so the failure path can be tested.

## Ansible (same principles)

`ansible_lint` -> `ansible_check` (`--check --diff`) -> review -> approval -> `ansible_run` ->
idempotency validation (a second check run must report `changed=0`). Playbooks with destructive
extra-vars (`delete`, `destroy`, `absent`) are classified DANGEROUS.
