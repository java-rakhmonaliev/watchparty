terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State bucket is bootstrapped once by hand (see DEVOPS.md Phase 1).
  # Locking uses S3's native lockfile — no DynamoDB table needed.
  backend "s3" {
    bucket       = "watchparty-tfstate-804223629120"
    key          = "infra.tfstate"
    region       = "eu-central-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "watchparty"
      ManagedBy = "terraform"
    }
  }
}
