locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "edxapp"
}

variable "business_unit" {
  type    = string
  default = "operations"
}

variable "build_environment" {
  type    = string
  default = "mitxonline-qa"
}

variable "edx_release_name" {
  type    = string
  default = "master"
}

variable "edx_platform_version" {
  type    = string
  default = "release"
}

# Allowed values are mitxonline, xpro, mitx, or mitx-staging
variable "installation_target" {
  type    = string
  default = "mitxonline"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type = string
}

source "amazon-ebs" "edxapp" {
  ami_description         = "Deployment image for Open edX ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "edxapp-${var.node_type}-${var.installation_target}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "m5.xlarge"
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "edx-${var.node_type}"
  }
  snapshot_tags = {
    Name           = "${local.app_name}-${var.node_type}-ami"
    OU             = "${var.business_unit}"
    app            = "${local.app_name}"
    purpose        = "${local.app_name}-${var.node_type}"
    edxapp_release = "${var.edx_platform_version}"
  }
  source_ami_filter {
    filters = {
      name                = "edxapp-${var.node_type}-${var.edx_platform_version}-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["610119931565"]
  }
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"
  subnet_filter {
    filters = {
      "tag:Environment" : var.build_environment
    }
    random = true
  }
  tags = {
    Name           = "${local.app_name}-${var.node_type}-${var.edx_platform_version}"
    OU             = "${var.business_unit}"
    app            = "${local.app_name}"
    deployment     = "${var.installation_target}"
    purpose        = "${local.app_name}-${var.node_type}"
    edxapp_release = "${var.edx_platform_version}"
  }
}

build {
  sources = [
    "source.amazon-ebs.edxapp",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem",
    ]
  }

  dynamic "provisioner" {
    for_each = ((var.installation_target == "mitx" || var.installation_target == "mitx-staging") && var.node_type == "web") ? ["this"] : []
    labels   = ["shell"]
    content {
      environment_vars = [
        "EDX_ANSIBLE_BRANCH=${var.edx_platform_version}",
      ]
      inline = [
        "openssl req -new -newkey rsa:2048 -days 365 -nodes -x509 -keyout /tmp/edxapp.key -out /tmp/edxapp.cert -subj '/C=US/ST=MA/L=Cambridge/O=MIT Open Learning/OU=Engineering/CN=edxapp.example.com'",
        "cd /tmp && git clone https://github.com/edx/configuration --depth 1 --branch $EDX_ANSIBLE_BRANCH",
        "cd /tmp/configuration && python3 -m venv .venv && .venv/bin/pip install wheel && .venv/bin/pip install -r requirements.txt",
      ]
    }
  }
  dynamic "provisioner" {
    for_each = ((var.installation_target == "mitx" || var.installation_target == "mitx-staging") && var.node_type == "web") ? ["this"] : []
    labels   = ["ansible-local"]

    content {
      playbook_file     = "${path.root}/files/edxapp_web_residential_playbook.yml"
      command           = "/tmp/configuration/.venv/bin/ansible-playbook --extra-vars 'EDX_PLATFORM_VERSION=${var.edx_platform_version}' --skip-tags 'manage:app-users'"
      staging_directory = "/tmp/configuration/playbooks/"
    }
  }

  provisioner "shell" {
    # Addresses change in latest git due to recent CVE
    # https://github.blog/2022-04-12-git-security-vulnerability-announced/
    inline = ["sudo git config --global --add safe.directory *"]
  }

  provisioner "shell-local" {
    environment_vars = [
      "NODE_TYPE=${var.node_type}",
      "EDX_RELEASE_NAME=${var.edx_release_name}",
      "EDX_INSTALLATION=${var.installation_target}",
    ]
    inline = [
      "pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"
    ]
  }
}
