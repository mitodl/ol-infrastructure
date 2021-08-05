locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name = "vault"
}

variable "build_environment" {
  type = string
  default = "operations-qa"
}

source "amazon-ebs" "vault" {
  ami_description         = "Deployment image for Vault server generated at ${local.timestamp}"
  ami_name                = "vault-server-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "vault-server"
  }
  snapshot_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-server"
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
    Name    = "vault-server"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "vault-server"
  }
}

build {
  sources = ["source.amazon-ebs.vault"]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session-${build.ID}.pem",
      "chmod 600 /tmp/packer-session-${build.ID}.pem"
    ]
  }
  provisioner "shell-local" {
    except = ["docker.vault"]
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} ${path.root}/deploy.py"]
  }
  provisioner "shell" {
    inline = [
      "sudo mkdir -p /etc/vault/ssl/",
      "sudo openssl req -new -newkey rsa:2048 -days 365 -nodes -x509 -keyout /etc/vault/ssl/vault.key -out /etc/vault/ssl/vault.cert -subj '/C=US/ST=MA/L=Cambridge/O=MIT Open Learning/OU=Engineering/CN=vault.service.consul'",
      "sudo chown -R vault:vault /etc/vault/"
    ]
  }
  # provisioner "shell-local" {
  #   except = ["docker.vault"]
  #   inline = ["py.test --ssh-identity-file=/tmp/packer-session.pem --hosts='ssh://${build.User}@${build.Host}:${build.Port}' ${path.root}/test_deploy.py"]
  # }
}
