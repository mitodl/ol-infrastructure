locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "docker"
}

variable "build_environment" {
  type    = string
  default = "operations-qa"
}

source "amazon-ebs" "docker" {
  ami_description         = "Deployment image for docker"
  ami_name                = "docker-web"
  ami_virtualization_type = "hvm"
  force_deregister        = true
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU  = "${local.business_unit}"
    app = "${local.app_name}"
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
      "tag:Environment" : var.build_environment
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

source "docker" "docker" {
  image      = "debian:trixie"
  discard    = true
  privileged = true
  changes = [
    "RUN ulimit -n 65536",
    "USER docker",
    "WORKDIR /opt/docker",
    "ENTRYPOINT /opt/docker/bin/docker"
  ]
}

build {
  sources = [
    "source.amazon-ebs.docker",
    "source.docker.docker",
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
    except = ["docker.docker"]
    inline = ["pyinfra -y --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session.pem ${build.Host} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    except = ["docker.docker"]
    inline = ["py.test --ssh-identity-file=/tmp/packer-session.pem --hosts='ssh://${build.User}@${build.Host}:${build.Port}' ${path.root}/test_docker_build.py"]
  }
  provisioner "shell-local" {
    only   = ["docker.docker"]
    inline = ["pyinfra @docker/${build.ID} ${path.root}/sample_deploy.py"]
  }
  provisioner "shell-local" {
    only   = ["docker.docker"]
    inline = ["py.test --hosts=docker://${build.ID} ${path.root}/test_docker_build.py"]
  }
}
