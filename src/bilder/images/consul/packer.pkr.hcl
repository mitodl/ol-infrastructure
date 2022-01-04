source "amazon-ebs" "consul" {
  ami_description         = "Deployment image for Consul server generated at ${local.timestamp}"
  ami_name                = "consul-server-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU  = local.business_unit
    app = local.app_name
  }
  snapshot_tags = {
    OU  = local.business_unit
    app = local.app_name
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
  tags = {
    Name = "${local.app_name}-ami"
    OU   = local.business_unit
    app  = local.app_name
  }
}

build {
  sources = [
    "source.amazon-ebs.consul",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session.pem",
      "chmod 600 /tmp/packer-session.pem"
    ]
  }
  provisioner "shell-local" {
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session.pem ${build.Host} ${path.root}/deploy.py"]
  }
}
