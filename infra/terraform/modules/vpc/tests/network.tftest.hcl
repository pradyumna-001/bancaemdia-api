mock_provider "aws" {}

run "two_azs_and_separate_nat_gateways" {
  command = plan
  variables {
    name          = "bancaemdia-staging"
    cidr          = "10.40.0.0/16"
    azs           = ["us-east-1a", "us-east-1b"]
    public_cidrs  = ["10.40.0.0/24", "10.40.1.0/24"]
    private_cidrs = ["10.40.10.0/24", "10.40.11.0/24"]
    enable_nat    = true
    tags          = {}
  }
  assert {
    condition     = length(aws_subnet.public) == 2 && length(aws_subnet.private) == 2 && length(aws_nat_gateway.this) == 2
    error_message = "The primary VPC needs two subnets of each type and one NAT gateway per AZ."
  }
}

run "dr_vpc_has_no_nat" {
  command = plan
  variables {
    name          = "bancaemdia-staging-dr"
    cidr          = "10.41.0.0/16"
    azs           = ["us-west-2a", "us-west-2b"]
    public_cidrs  = ["10.41.0.0/24", "10.41.1.0/24"]
    private_cidrs = ["10.41.10.0/24", "10.41.11.0/24"]
    enable_nat    = false
    tags          = {}
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 0 && length(aws_subnet.private) == 2
    error_message = "The recovery-only VPC must not allocate NAT gateways."
  }
}
