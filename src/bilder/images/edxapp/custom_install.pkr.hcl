locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "edxapp"
}

variable "build_environment" {
  type    = string
  default = "mitxonline-qa"
}

variable "edx_platform_version" {
  type    = string
  default = "release"
}

# Allowed values are mitxonline, xpro, mitx, or mitx-staging
variable "installation_target" {
  type    = string
  default = "mitxonline"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type = string
}

source "amazon-ebs" "edxapp" {
  ami_description         = "Deployment image for Open edX ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "edxapp-${var.node_type}-${var.installation_target}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name = "/dev/sda1"
    volume_size = 25
  }
  run_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "edx-${var.node_type}"
  }
  snapshot_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  # Base all builds off of the most recent Ubuntu 20.04 image built by the Canonical organization.
  source_ami_filter {
    filters = {
      name                = "edxapp-${var.node_type}-${var.edx_platform_version}-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["610119931565"]
  }
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"
  subnet_filter {
    filters = {
      "tag:Environment" : var.build_environment
    }
    random = true
  }
  tags = {
    Name    = "${local.app_name}-${var.node_type}-${var.edx_platform_version}"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
}

build {
  sources = [
    "source.amazon-ebs.edxapp",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem"
    ]
  }

  provisioner "shell-local" {
    environment_vars = ["NODE_TYPE=${var.node_type}", "EDX_INSTALLATION=${var.installation_target}"]
    inline           = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} ${path.root}/deploy.py"]
  }
}
