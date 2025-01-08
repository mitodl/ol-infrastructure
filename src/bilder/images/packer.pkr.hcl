source "amazon-ebs" "third-party" {
  ami_description         = "Deployment image for ${title(var.app_name)} ${var.node_type} generated at ${local.timestamp}"
  ami_name                = "${var.app_name}-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 25
    delete_on_termination = true
  }
  run_volume_tags = {
    Name    = "${var.app_name}-${var.node_type}"
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-${var.node_type}"
  }
  snapshot_tags = {
    Name    = "${var.app_name}-${var.node_type}-ami"
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-${var.node_type}"
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
    Name    = "${var.app_name}-${var.node_type}"
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-${var.node_type}"
  }
  run_tags = {
    Name    = "${var.app_name}-${var.node_type}"
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-${var.node_type}"
  }
}

build {
  sources = ["source.amazon-ebs.third-party"]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session-${build.ID}.pem",
      "chmod 600 /tmp/packer-session-${build.ID}.pem"
    ]
  }
  provisioner "shell-local" {
    environment_vars = ["NODE_TYPE=${var.node_type}"]
    inline           = ["pyinfra -y --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root}/${var.app_name} deploy.py"]
  }

  # TODO: move to vault pyinfra
  provisioner "file" {
    source      = "${path.root}/vault/files/vault_env_script.sh"
    destination = "/tmp/vault_env_script.sh"
  }
  provisioner "shell" {
    inline = ["sudo mv /tmp/vault_env_script.sh /var/lib/cloud/scripts/per-instance/vault_env_script.sh"]
  }
}
