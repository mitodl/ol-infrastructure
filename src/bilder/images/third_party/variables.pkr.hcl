locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
}

variable "app_name" {
  type        = string
  description = "The name of the third party application."

  validation {
    condition     = contains(["vault", "consul"], var.app_name)
    error_message = "Valid app_name inputs are 'vault' or 'consul'."
  }
}

variable "build_environment" {
  type        = string
  description = "The build environment."
  default     = "operations-ci"
}

variable "business_unit" {
  type        = string
  description = "The business unit."
  default     = "operations"
}
