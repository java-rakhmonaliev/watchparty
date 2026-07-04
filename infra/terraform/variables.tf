variable "region" {
  type    = string
  default = "eu-central-1" # Frankfurt
}

variable "project" {
  type    = string
  default = "watchparty"
}

variable "instance_type" {
  type    = string
  default = "t4g.small" # arm64 Graviton — cheapest thing that comfortably runs the stack
}

variable "github_repo" {
  description = "GitHub repo (owner/name) allowed to deploy via OIDC"
  type        = string
  default     = "java-rakhmonaliev/watchparty"
}

variable "turn_min_port" {
  type    = number
  default = 49160
}

variable "turn_max_port" {
  type    = number
  default = 49200
}
