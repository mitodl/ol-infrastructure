source "amazon-ebs" "concourse" {
  ami_description         = "Deployment image for Concourse ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "concourse-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = local.business_unit
    app     = local.app_name
    purpose = "concourse-${var.node_type}"
  }
  snapshot_tags = {
    OU      = local.business_unit
    app     = local.app_name
    purpose = "${local.app_name}-${var.node_type}"
  }
  # Base all builds off of the most recent Debian 10 image built by the Debian organization.
  source_ami_filter {
    filters = {
      name                = "debian-11-amd64*"
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
  run_tags = {
    Name    = "${local.app_name}-${var.node_type}"
    OU      = local.business_unit
    app     = local.app_name
    purpose = "${local.app_name}-${var.node_type}"
  }
  tags = {
    Name    = "${local.app_name}-${var.node_type}"
    OU      = local.business_unit
    app     = local.app_name
    purpose = "${local.app_name}-${var.node_type}"
  }
}

source "docker" "concourse" {
  image  = "debian:buster"
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
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem"
    ]
  }
  provisioner "shell-local" {
    except           = ["docker.concourse"]
    environment_vars = ["NODE_TYPE=${var.node_type}"]
    inline           = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} ${path.root}/deploy.py"]
  }
  # provisioner "shell-local" {
  #   except = ["docker.concourse"]
  #   inline = ["py.test --ssh-identity-file=/tmp/packer-session.pem --hosts='ssh://${build.User}@${build.Host}:${build.Port}' ${path.root}/test_deploy.py"]
  # }
  provisioner "shell-local" {
    only   = ["docker.concourse"]
    inline = ["pyinfra @docker/${build.ID} ${path.root}/deploy.py"]
  }
  # provisioner "shell-local" {
  #   only = ["docker.concourse"]
  #   inline = ["py.test --hosts=docker://${build.ID} ${path.root}/test_deploy.py"]
  # }
}
