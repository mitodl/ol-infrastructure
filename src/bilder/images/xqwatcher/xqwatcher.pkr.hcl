packer {
  required_plugins {
    ansible = {
      source  = "github.com/hashicorp/ansible"
      version = "~> 1"
    }
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1"
    }
  }
}

locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "open-edx-xqwatcher-server"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}

variable "business_unit" {
  type    = string
  default = "operations"
}

variable "node_type" {
  type    = string
  default = "server"
}

variable "ol_ansible_branch" {
  type    = string
  default = "md/issue_2326"
}

source "amazon-ebs" "xqwatcher" {
  ami_description         = "Deployment image for xqwatcher server generated at ${local.timestamp}"
  ami_name                = "open-edx-xqwatcher-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}"
  }
  snapshot_tags = {
    Name            = "${local.app_name}-ami"
    OU              = "${var.business_unit}"
    app             = "${local.app_name}"
    purpose         = "${local.app_name}"
  }
  
  # Base all builds off of the most recent Debian 12 image built by the Debian organization.
  source_ami_filter {
    filters = {
      name                = "debian-12-amd64*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["136693071363"]
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
    OU              = var.business_unit
    app             = local.app_name
    purpose         = "${local.app_name}"
    framework       = "native"
  }
}

build {
  sources = [
    "source.amazon-ebs.xqwatcher",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session-${build.ID}.pem",
      "chmod 600 /tmp/packer-session-${build.ID}.pem"
    ]
  }

  # Run the pre-ansible configuration / setup via py-infra
  provisioner "shell-local" {
    inline = ["pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"]
  }
}