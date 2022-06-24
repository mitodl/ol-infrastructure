locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "forum"
}

variable "build_environment" {
  type        = string
  default     = "operations-ci"
}

variable "business_unit" {
  type        = string
  default     = "operations"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type    = string
  default = "server"
}

variable "deployment" {
  type    = string
}

source "amazon-ebs" "forum" {
  ami_description         = "Deployment image for Forum server generated at ${local.timestamp}"
  ami_name                = "forum-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 25
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
    purpose = "${local.app_name}-${var.node_type}"
  }
  snapshot_tags = {
    Name           = "${local.app_name}-${var.node_type}-ami"
    OU             = "${var.business_unit}"
    app            = "${local.app_name}"
    purpose        = "${local.app_name}-${var.node_type}"
  }
  # Base all builds off of the most recent docker_baseline_ami built by us, based of Debian 11
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
    Name    = "${local.app_name}-${var.node_type}"
    OU      = var.business_unit
    app     = local.app_name
    purpose = "${local.app_name}-${var.node_type}"
    deployment = "${var.deployment}"
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
    environment_vars = ["NODE_TYPE=${var.node_type}", "DEPLOYMENT=${var.deployment}"]
    inline           = ["pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"]
  }
}
