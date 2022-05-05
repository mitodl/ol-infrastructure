locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
}

variable "app_name" {
  type        = string
  description = "The name of the third party application."

  validation {
    condition     = contains(["vector_log_proxy", "vault", "consul", "concourse", "tika"], var.app_name)
    error_message = "Valid app_name inputs are 'vector_log_proxy', 'vault', 'consul', or 'concourse'."
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

variable "node_type" {
  type        = string
  default     = "server"
  description = "The node type for the image. Available options are 'web' or 'worker' for Concourse and 'server' for Consul, Tika, Vault and vector_logr_proxy."

  validation {
    condition     = contains(["web", "worker", "server"], var.node_type)
    error_message = "Valid node_type inputs are 'web', 'worker', or 'server'."
  }
}
