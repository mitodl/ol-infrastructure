locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "mitx-online"
  app_name = "consul"
}

variable "CONSUL_VERSION" {
  type = string
  default = "1.10.0"
}

variable "build_environment" {
  type = string
  default = "mitxonline-qa"
}

source "amazon-ebs" "concourse" {
  ami_description         = "Deployment image for Consul server generated at ${local.timestamp}"
  ami_name                = "consul"
  ami_virtualization_type = "hvm"
  force_deregister        = true
  force_delete_snapshot   = true
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
  }
  snapshot_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
  }
  # Base all builds off of the most recent Debian 10 image built by the Debian organization.
  source_ami_filter {
    filters = {
      name                = "debian-10-amd64*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["136693071363"]
  }
  ssh_username = "admin"
  subnet_filter {
    filters = {
          "tag:Environment": var.build_environment
    }
    random = true
  }
  tags = {
    Name    = "${local.app_name}-ami"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
  }
}

source "docker" "consul" {
  image = "debian:buster"
  commit = true
}

build {
  sources = [
    "source.amazon-ebs.consul",
    "source.docker.consul",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session.pem",
      "chmod 600 /tmp/packer-session.pem"
    ]
  }
  provisioner "shell-local" {
    except = ["docker.consul"]
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session.pem ${build.Host} ${path.root}/deploy.py"]
  }

  provisioner "shell-local" {
    only = ["docker.consul"]
    inline = ["pyinfra @docker/${build.ID} ${path.root}/deploy.py"]
  }
}
