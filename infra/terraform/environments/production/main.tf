terraform {
  required_version = ">= 1.14.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.42.0, < 7.0.0"
    }
  }
  backend "s3" {}
}

provider "aws" { region = var.region }
provider "aws" {
  alias  = "dr"
  region = var.dr_region
}

module "stack" {
  source          = "../.."
  providers       = { aws = aws, aws.dr = aws.dr }
  environment     = "production"
  region          = var.region
  dr_region       = var.dr_region
  api_domain      = var.api_domain
  route53_zone_id = var.route53_zone_id
  image_uri       = var.image_uri
  deploy_enabled  = var.deploy_enabled
  notification    = var.notification
  tags            = var.tags
}
