locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "concourse"
}

variable "build_environment" {
  type    = string
  default = "operations-qa"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type = string
}

source "amazon-ebs" "concourse" {
  ami_description         = "Deployment image for Concourse ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "concourse-web"
  ami_virtualization_type = "hvm"
  force_deregister        = true
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "concourse-${var.node_type}"
  }
  snapshot_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
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
  ssh_username = "admin"
  subnet_filter {
    filters = {
      "tag:Environment" : var.build_environment
    }
    random = true
  }
  tags = {
    Name    = "${local.app_name}-${var.node_type}"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
}

source "docker" "concourse" {
  image  = "debian:trixie"
  commit = true
  changes = [
    "USER concourse",
    "WORKDIR /opt/concourse",
    "ENTRYPOINT /opt/concourse/bin/concourse ${var.node_type}"
  ]
}

build {
  sources = [
    "source.amazon-ebs.concourse",
    "source.docker.concourse",
  ]

  provisioner "shell-local" {
    inline = [
      "echo ${build.name}",
      "echo ${build.ID}",
      "echo ${build.ConnType}",
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session.pem",
      "chmod 600 /tmp/packer-session.pem"
    ]
  }
  provisioner "shell-local" {
    except = ["docker.concourse"]
    inline = ["pyinfra -y --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session.pem ${build.Host} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    except = ["docker.concourse"]
    inline = ["py.test --ssh-identity-file=/tmp/packer-session.pem --hosts='ssh://${build.User}@${build.Host}:${build.Port}' ${path.root}/test_concourse_build.py"]
  }
  provisioner "shell-local" {
    only   = ["docker.concourse"]
    inline = ["pyinfra @docker/${build.ID} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    only   = ["docker.concourse"]
    inline = ["py.test --hosts=docker://${build.ID} ${path.root}/test_concourse_build.py"]
  }
}
