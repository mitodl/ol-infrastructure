locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "consul"
}

variable "CONSUL_VERSION" {
  type    = string
  default = "1.10.0"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}
