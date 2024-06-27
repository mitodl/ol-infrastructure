locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "edx_notes"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}

variable "business_unit" {
  type    = string
  default = "missing"
  validation {
    condition     = contains(["mitx", "mitx-staging", "mitxonline", "xpro"], var.business_unit)
    error_message = "Valid business_unit inputs are 'mitx', 'mitx-staging', 'mitxonline', or 'xpro'."
  }
}

variable "node_type" {
  type    = string
  default = "server"
}

variable "deployment" {
  type = string
}

variable "openedx_release" {
  type = string
}

source "amazon-ebs" "edx_notes" {
  ami_description         = "Deployment image for edx-notes application generated at ${local.timestamp}"
  ami_name                = "edx_notes-${var.business_unit}-${var.openedx_release}-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-${var.openedx_release}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.openedx_release}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-${var.openedx_release}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.openedx_release}"
  }
  snapshot_tags = {
    Name            = "${local.app_name}-${var.openedx_release}-ami"
    OU              = "${var.business_unit}"
    app             = "${local.app_name}"
    purpose         = "${local.app_name}-${var.openedx_release}"
    openedx_release = var.openedx_release
  }
  # Base all builds off of the most recent docker_baseline_ami built by us, based of Debian 12

  source_ami_filter {
    filters = {
      name                = "docker_baseline_ami-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["610119931565"]
  }

  ssh_username  = "admin"
  ssh_interface = "public_ip"
  subnet_filter {
    filters = {
      "tag:Environment" : var.build_environment
    }
    random = true
  }
  tags = {
    Name            = "${local.app_name}-${var.openedx_release}"
    OU              = var.business_unit
    app             = local.app_name
    purpose         = "${local.app_name}-${var.openedx_release}"
    openedx_release = var.openedx_release
  }
}

build {
  sources = [
    "source.amazon-ebs.edx_notes",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session-${build.ID}.pem",
      "chmod 600 /tmp/packer-session-${build.ID}.pem"
    ]
  }

  provisioner "shell-local" {
    environment_vars = [
      "NODE_TYPE=${var.node_type}",
      "DEPLOYMENT=${var.deployment}",
      "OPENEDX_RELEASE=${var.openedx_release}"
    ]
    inline = ["pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"]
  }
}
