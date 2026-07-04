# The EIP is standalone so its address can be baked into user_data (the
# instance needs to know its own public hostname for TLS and TURN) without a
# dependency cycle; the association attaches it after boot.
resource "aws_eip" "web" {
  domain = "vpc"
  tags   = { Name = var.project }
}

resource "aws_instance" "web" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region        = var.region
    registry      = local.registry
    ecr_image     = "${aws_ecr_repository.app.repository_url}:latest"
    domain        = local.domain
    eip           = aws_eip.web.public_ip
    turn_min_port = var.turn_min_port
    turn_max_port = var.turn_max_port
  })
  user_data_replace_on_change = true

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = { Name = var.project }
}

resource "aws_eip_association" "web" {
  instance_id   = aws_instance.web.id
  allocation_id = aws_eip.web.id
}
