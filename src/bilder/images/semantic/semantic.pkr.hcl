locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "semantic"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}

variable "business_unit" {
  type    = string
  default = "open-courseware"
}

variable "node_type" {
  type    = string
  default = "server"
}


source "amazon-ebs" "semantic" {
  ami_description         = "Deployment image for semantic POC application generated at ${local.timestamp}"
  ami_name                = "semantic-${var.business_unit}-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-POC"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-POC"
  }
  snapshot_tags = {
    Name            = "${local.app_name}-ami"
    OU              = "${var.business_unit}"
    app             = "${local.app_name}"
    purpose         = "${local.app_name}-POC"
  }
  # Base all builds off of the most recent docker_baseline_ami built by us, based of Debian 11

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
    Name            = "${local.app_name}"
    OU              = "${var.business_unit}"
    app             = local.app_name
    purpose         = "${local.app_name}"
  }
}

build {
  sources = [
    "source.amazon-ebs.semantic",
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
    ]
    inline = ["pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"]
  }
}
