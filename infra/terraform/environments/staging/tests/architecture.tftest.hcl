mock_provider "aws" {
  override_during = plan
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012"
      domain_validation_options = [{
        domain_name           = "staging-api.example.com"
        resource_record_name  = "_acme.staging-api.example.com"
        resource_record_type  = "CNAME"
        resource_record_value = "validation.example.com"
      }]
    }
  }
  mock_resource "aws_acm_certificate_validation" {
    defaults = { certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/staging/123abc" }
  }
  mock_resource "aws_db_instance" {
    defaults = { master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db" }] }
  }
}

mock_provider "aws" {
  alias           = "dr"
  override_during = plan
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b"] }
  }
}

run "foundation_without_services" {
  command = plan
  variables {
    region          = "us-east-1"
    dr_region       = "us-west-2"
    api_domain      = "staging-api.example.com"
    route53_zone_id = "Z0123456789"
    deploy_enabled  = false
  }
  assert {
    condition     = length(module.stack.ecs_service_names) == 0
    error_message = "Services must stay disabled until image, secrets and migrations exist."
  }
  assert {
    condition     = module.stack.rds_dr_identifier != ""
    error_message = "The stack must include a cross-region recovery replica."
  }
}

run "deploys_four_distinct_roles" {
  command = plan
  variables {
    region          = "us-east-1"
    dr_region       = "us-west-2"
    api_domain      = "staging-api.example.com"
    route53_zone_id = "Z0123456789"
    deploy_enabled  = true
    image_uri       = "123456789012.dkr.ecr.us-east-1.amazonaws.com/bancaemdia-staging-api@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }
  assert {
    condition     = length(module.stack.ecs_service_names) == 4
    error_message = "API, extraction, materialization, and beat services must all be created."
  }
}
