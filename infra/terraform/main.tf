data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Latest Amazon Linux 2023 for arm64 (Graviton).
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  registry   = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  # No bought domain yet: <EIP>.sslip.io resolves to the EIP for free, which
  # gives Caddy a real Let's Encrypt cert (getUserMedia needs HTTPS).
  domain = "${aws_eip.web.public_ip}.sslip.io"
}
