# AWS agent

## Principles

* Diagnosis uses read-only APIs only: `aws_describe` accepts `describe-*`, `list-*`, `get-*`,
  `lookup-*`, `search-*`, `query`, `scan`, `head-*`, `batch-get-*`, `filter-*`, `simulate-*`,
  `test-*`, `check-*` and refuses everything else.
* `aws_modify` (modify/update/create/put/start/stop/...) is DEPLOY and needs approval.
* `aws_destroy` (delete/terminate/deregister/remove, and every IAM change) is DESTROY and needs
  explicit approval; rollback is stated as NOT AVAILABLE unless a backup/IaC restore exists.
* The first call is always `aws_identity` (sts get-caller-identity). The account is matched
  against `environments.<env>.aws_accounts`; a production account escalates the task environment.

## Services

EC2, ECS, EKS, S3, IAM, VPC, ELB/ALB/NLB (elbv2), Route53, CloudWatch (metrics + logs), Lambda,
RDS, ECR, Secrets Manager (names only), SSM, CloudFormation, autoscaling, SNS/SQS, DynamoDB,
KMS, ACM, CloudTrail, Cost Explorer/pricing.

## Configuration

```yaml
aws_profile: devops-readonly
aws_region: eu-west-1
environments:
  production: { aws_accounts: ["123456789012"] }
```

Credentials come from the standard AWS credential chain (profile, environment, SSO, instance
role). The harness passes `AWS_*` variables to the `aws` CLI only, never into prompts or logs.

## Failure handling

`ExpiredToken` / `InvalidClientTokenId` -> `auth` (no retry, task BLOCKED with the reason);
`AccessDenied` / `UnauthorizedOperation` -> `permission` (no retry; suggests an alternative
read-only API or human action); `Throttling` -> bounded backoff.

## Cost awareness

Changes that add capacity (node groups, instance sizes, replicas) carry a cost note in the plan.
Without `pricing:` data in the config the note is qualitative and explicitly marked as an estimate.
