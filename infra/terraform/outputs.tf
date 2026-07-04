output "url" {
  value       = "https://${local.domain}"
  description = "The app (real Let's Encrypt cert via sslip.io — no bought domain needed)"
}

output "public_ip" {
  value = aws_eip.web.public_ip
}

output "instance_id" {
  value = aws_instance.web.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "Set this as the AWS_ROLE_ARN secret in the GitHub repo"
}
