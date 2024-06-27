locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "open-edx-forum-server"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}

variable "business_unit" {
  type    = string
  default = "operations"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
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

source "amazon-ebs" "forum" {
  ami_description         = "Deployment image for Forum server generated at ${local.timestamp}"
  ami_name                = "open-edx-forum-${var.deployment}-${var.openedx_release}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-${var.deployment}-${var.openedx_release}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.deployment}-${var.openedx_release}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-${var.deployment}-${var.openedx_release}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.deployment}-${var.openedx_release}"
  }
  snapshot_tags = {
    Name            = "${local.app_name}-${var.deployment}-${var.openedx_release}-ami"
    OU              = "${var.business_unit}"
    app             = "${local.app_name}"
    purpose         = "${local.app_name}-${var.deployment}-${var.openedx_release}"
    openedx_release = var.openedx_release
  }
  # Base all builds off of the most recent docker_baseline_ami built by us, based of Debian 12
  source_ami_filter {
    filters = {
      name                = "docker_baseline_ami-${var.node_type}-*"
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
    Name            = "${local.app_name}-${var.deployment}-${var.openedx_release}"
    OU              = var.business_unit
    app             = local.app_name
    purpose         = "${local.app_name}-${var.deployment}-${var.openedx_release}"
    deployment      = "${var.deployment}"
    openedx_release = var.openedx_release
  }
}

build {
  sources = [
    "source.amazon-ebs.forum",
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
