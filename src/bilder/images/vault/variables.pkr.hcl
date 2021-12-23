locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "vault"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}
