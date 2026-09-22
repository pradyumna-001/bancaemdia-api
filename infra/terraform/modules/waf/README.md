# Regional AWS WAF module

This module creates a regional AWS WAFv2 Web ACL and immediately associates it
with one Application Load Balancer. The policy has
AWS `AWSManagedRulesCommonRuleSet` only. It deliberately has no custom IP or
rate-based rule because per-user throttling belongs to the API.

```hcl
module "waf" {
  source = "../../modules/waf"

  name_prefix           = "bancaemdia-staging"
  protected_resource_arn = module.alb.arn

  tags = {
    Environment = "staging"
    Service     = "bancaemdia-api"
  }
}
```

`protected_resource_arn` is required and accepts only a regional ALB ARN. The
module therefore cannot produce an unattached Web ACL. API Gateway is rejected
because its fixed 10 MB request cap cannot carry this API's supported 50 MB
uploads. Configure the AWS provider in the same region as the ALB.

## Large request bodies

The Common Rule Set's `SizeRestrictions_BODY` rule blocks request bodies over
8 KiB, and an ALB only exposes the first 8 KiB to AWS WAF. That conflicts with
the API's intentional 50 MB upload endpoint. The module applies the unmodified
managed group to every other request and a second, upload-scoped instance to
`POST /api/v1/upload`, where only `SizeRestrictions_BODY` changes to `count`.
Every other managed action remains enforced, while the API owns the 50 MB body
cap.

CloudWatch metrics are always enabled. Request sampling defaults to off to avoid
retaining potentially sensitive request data; enable it only after reviewing
the environment's data-retention policy.

The complete ALB/ECS environment is intentionally outside this module and is
planned as the Week 4 infrastructure work. That stack should pass its ALB ARN
directly to this module as shown above.

That environment must also set `RATE_LIMIT_TRUSTED_PROXY_CIDRS` to the private
subnet CIDRs used by the ALB. The API ignores `X-Forwarded-For` from every peer
outside those explicitly trusted networks, so authentication limits cannot be
spoofed by a direct client or accidentally collapse every client into one ALB
address.

References:

- [AWS Common Rule Set](https://docs.aws.amazon.com/waf/latest/developerguide/aws-managed-rule-groups-baseline.html)
- [Terraform Web ACL association](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/wafv2_web_acl_association)

Run `terraform init -backend=false`, `terraform fmt -check -recursive`,
`terraform validate`, and `terraform test` from this directory to validate the
module. The mocked tests require Terraform 1.7 or newer and do not contact AWS.
