locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name      = "edxapp"
}

variable "build_environment" {
  type    = string
  default = "mitxonline-qa"
}

variable "edx_platform_version" {
  type    = string
  default = "release"
}

variable "edx_ansible_branch" {
  type    = string
  default = "master"
}

variable "edx_release_name" {
  type    = string
  default = "master"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type = string
}

source "amazon-ebs" "edxapp" {
  ami_description         = "Deployment image for Open edX ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "edxapp-${var.node_type}-${var.edx_platform_version}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "m5.xlarge"
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 25
    delete_on_termination = true
  }
  run_tags = {
    Name    = "${local.app_name}-${var.node_type}-packer-builder"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  run_volume_tags = {
    Name    = "${local.app_name}-${var.node_type}"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "edx-${var.node_type}"
  }
  snapshot_tags = {
    Name    = "${local.app_name}-${var.node_type}-ami"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  # Base all builds off of the most recent Ubuntu 20.04 image built by the Canonical organization.
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]
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
    Name    = "${local.app_name}-${var.node_type}"
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
}

build {
  sources = [
    "source.amazon-ebs.edxapp",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem"
    ]
  }
  provisioner "shell-local" {
    environment_vars = [
      "DEBIAN_FRONTEND=noninteractive",
      "EDX_RELEASE_NAME=${var.edx_release_name}"
    ]
    inline = [
      "sleep 15",
      "pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} --chdir ${path.root} prebuild.py"
    ]
  }
  provisioner "shell" {
    # Addresses change in latest git due to recent CVE
    # https://github.blog/2022-04-12-git-security-vulnerability-announced/
    inline = ["sudo git config --global --add safe.directory *"]
  }
  provisioner "shell" {
    environment_vars = [
      "EDX_ANSIBLE_BRANCH=${var.edx_ansible_branch}",
    ]
    inline = [
      "openssl req -new -newkey rsa:2048 -days 365 -nodes -x509 -keyout /tmp/edxapp.key -out /tmp/edxapp.cert -subj '/C=US/ST=MA/L=Cambridge/O=MIT Open Learning/OU=Engineering/CN=edxapp.example.com'",
      "cd /tmp && git clone https://github.com/edx/configuration --depth 1 --branch $EDX_ANSIBLE_BRANCH",
      "cd /tmp/configuration && python3 -m venv .venv && .venv/bin/pip install wheel && .venv/bin/pip install -r requirements.txt"
    ]
  }
  provisioner "ansible-local" {
    playbook_file     = "${path.root}/files/edxapp_${var.node_type}_playbook.yml"
    command           = "/tmp/configuration/.venv/bin/ansible-playbook --extra-vars 'EDX_PLATFORM_VERSION=${var.edx_platform_version}'"
    staging_directory = "/tmp/configuration/playbooks/"
  }
}
