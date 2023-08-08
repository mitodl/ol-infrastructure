locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "edxapp"
}

variable "business_unit" {
  type    = string
  default = "operations"
}

variable "build_environment" {
  type    = string
  default = "mitxonline-qa"
}

variable "openedx_release" {
  type = string
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
  instance_type           = "m5.xlarge"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 20
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "edx-${var.node_type}"
  }
  snapshot_tags = {
    Name            = "${local.app_name}-${var.node_type}-ami"
    OU              = var.business_unit
    app             = local.app_name
    purpose         = "${local.app_name}-${var.node_type}"
    openedx_release = var.openedx_release
  }
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
    Name            = "${local.app_name}-${var.node_type}-${var.openedx_release}"
    OU              = var.business_unit
    app             = local.app_name
    deployment      = var.installation_target
    framework       = "docker"
    purpose         = "${local.app_name}-${var.node_type}"
    openedx_release = var.openedx_release
  }
}

build {
  sources = [
    "source.amazon-ebs.edxapp",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem",
    ]
  }

  provisioner "shell" {
    # Addresses change in latest git due to recent CVE
    # https://github.blog/2022-04-12-git-security-vulnerability-announced/
    inline = ["sudo git config --global --add safe.directory *"]
  }

  provisioner "shell-local" {
    environment_vars = [
      "NODE_TYPE=${var.node_type}",
      "OPENEDX_RELEASE=${var.openedx_release}",
      "EDX_INSTALLATION=${var.installation_target}",
    ]
    inline = [
      "pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"
    ]
  }
}
