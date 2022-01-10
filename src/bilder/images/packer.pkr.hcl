source "amazon-ebs" "third-party" {
  ami_description         = "Deployment image for ${title(var.app_name)} server generated at ${local.timestamp}"
  ami_name                = "${var.app_name}-server-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-server"
  }
  snapshot_tags = {
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-server"
  }
  # Base all builds off of the most recent Debian 11 image built by the Debian organization.
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
    Name    = "${var.app_name}-server"
    OU      = var.business_unit
    app     = var.app_name
    purpose = "${var.app_name}-server"
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
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} ${path.root}/${var.app_name}/deploy.py"]
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
