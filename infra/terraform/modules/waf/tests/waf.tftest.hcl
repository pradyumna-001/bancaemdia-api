mock_provider "aws" {}

variables {
  name_prefix = "bancaemdia-staging"
  protected_resource_arn = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/bancaemdia/0123456789abcdef"
  )
}

run "common_rules_are_attached" {
  command = plan

  assert {
    condition     = aws_wafv2_web_acl.this.scope == "REGIONAL"
    error_message = "The Web ACL must use regional scope for the ALB."
  }

  assert {
    condition = toset([for rule in aws_wafv2_web_acl.this.rule : rule.name]) == toset([
      "AWS-AWSManagedRulesCommonRuleSet",
      "AWS-AWSManagedRulesCommonRuleSet-Upload",
    ])
    error_message = "The Web ACL must contain only the default and upload-scoped managed groups."
  }

  assert {
    condition = alltrue([
      for rule in aws_wafv2_web_acl.this.rule :
      one(rule.statement).managed_rule_group_statement[0].name == "AWSManagedRulesCommonRuleSet" &&
      one(rule.statement).managed_rule_group_statement[0].vendor_name == "AWS"
    ])
    error_message = "Every top-level rule must use AWS's managed Common Rule Set."
  }

  assert {
    condition = (
      length(one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet"]).statement).managed_rule_group_statement[0].rule_action_override) == 0 &&
      toset([
        for statement in one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet"]).statement).managed_rule_group_statement[0].scope_down_statement[0].not_statement[0].statement[0].and_statement[0].statement :
        one(statement.byte_match_statement).search_string
      ]) == toset(["/api/v1/upload", "POST"])
    )
    error_message = "Every request except POST /api/v1/upload must retain the unmodified managed rule actions."
  }

  assert {
    condition = (
      length(one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet-Upload"]).statement).managed_rule_group_statement[0].rule_action_override) == 1 &&
      one(one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet-Upload"]).statement).managed_rule_group_statement[0].rule_action_override).name == "SizeRestrictions_BODY" &&
      length(one(one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet-Upload"]).statement).managed_rule_group_statement[0].rule_action_override).action_to_use[0].count) == 1 &&
      toset([
        for statement in one(one([for rule in aws_wafv2_web_acl.this.rule : rule if rule.name == "AWS-AWSManagedRulesCommonRuleSet-Upload"]).statement).managed_rule_group_statement[0].scope_down_statement[0].and_statement[0].statement :
        one(statement.byte_match_statement).search_string
      ]) == toset(["/api/v1/upload", "POST"])
    )
    error_message = "Only POST /api/v1/upload may change the managed body-size rule to count."
  }

  assert {
    condition     = aws_wafv2_web_acl_association.this.resource_arn == var.protected_resource_arn
    error_message = "The Web ACL must be associated with the requested regional resource."
  }

  assert {
    condition = (
      one(aws_wafv2_web_acl.this.visibility_config).sampled_requests_enabled == false &&
      alltrue([for rule in aws_wafv2_web_acl.this.rule : one(rule.visibility_config).sampled_requests_enabled == false])
    )
    error_message = "Request sampling must remain opt-in to avoid retaining sensitive payloads."
  }
}

run "rejects_api_gateway_stage_with_a_ten_mb_cap" {
  command = plan

  variables {
    protected_resource_arn = "arn:aws:apigateway:us-east-1::/restapis/a1b2c3d4/stages/staging"
  }

  expect_failures = [
    var.protected_resource_arn,
  ]
}

run "rejects_unprotected_resource_types" {
  command = plan

  variables {
    protected_resource_arn = "arn:aws:s3:::not-a-supported-waf-target"
  }

  expect_failures = [
    var.protected_resource_arn,
  ]
}
