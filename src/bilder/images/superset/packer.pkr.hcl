locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name = "superset"
}

variable "build_environment" {
  type = string
  default = "operations-qa"
}

source "amazon-ebs" "superset" {
  ami_description         = "Deployment image for Superset server generated at ${local.timestamp}"
  ami_name                = "superset"
  ami_virtualization_type = "hvm"
  force_deregister        = true
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "superset"
  }
  snapshot_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}"
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
          "tag:Environment": var.build_environment
    }
    random = true
  }
  tags = {
    Name    = "${local.app_name}"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}"
  }
}

source "docker" "superset" {
  image = "debian:bookworm"
  commit = true
}

build {
  sources = [
    "source.amazon-ebs.superset",
    "source.docker.superset",
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
    except = ["docker.superset"]
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session.pem ${build.Host} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    except = ["docker.superset"]
    inline = ["py.test --ssh-identity-file=/tmp/packer-session.pem --hosts='ssh://${build.User}@${build.Host}:${build.Port}' ${path.root}/test_superset_build.py"]
  }
  provisioner "shell-local" {
    only = ["docker.superset"]
    inline = ["pyinfra @docker/${build.ID} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    only = ["docker.superset"]
    inline = ["py.test --hosts=docker://${build.ID} ${path.root}/test_superset_build.py"]
  }
}
