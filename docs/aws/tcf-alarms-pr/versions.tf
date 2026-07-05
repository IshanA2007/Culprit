# Provider + Terraform version constraints for the tcf-alarms module.
#
# Pinned conservatively so this drops cleanly into theCourseForum's existing
# iac/ (which already targets the AWS provider v5 line). The archive provider
# is only used to zip the canary handler into the Synthetics source bundle.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
