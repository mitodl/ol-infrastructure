locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  business_unit = "operations"
  app_name = "edx-platform"
}

variable "ansible_branch" {
  type = string
  default = "master"
}

variable "build_environment" {
  type = string
  default = "mitxonline-qa"
}

# Available options are "web" or "worker". Used to determine which type of node to build an image for.
variable "node_type" {
  type = string
}

source "amazon-ebs" "edx-platform" {
  ami_description         = "Deployment image for Open edX ${var.node_type} server generated at ${local.timestamp}"
  ami_name                = "edx-platform-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
      device_name = "/dev/sda1"
      volume_size = 25
  }
  run_volume_tags = {
    OU      = "${local.business_unit}"
    app     = "${local.app_name}"
    purpose = "edx-${var.node_type}"
  }
  snapshot_tags = {
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
  ssh_username = "ubuntu"
  ssh_interface = "public_ip"
  subnet_filter {
    filters = {
          "tag:Environment": var.build_environment
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
    "source.amazon-ebs.edx-platform",
  ]

  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-${build.ID}.pem",
      "chmod 600 /tmp/packer-${build.ID}.pem"
    ]
  }
  provisioner "shell-local" {
    environment_vars = [
      "DEBIAN_FRONTEND=noninteractive"
    ]
    inline = [
      "pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} apt.packages packages='[\"git\", \"libmariadbclient-dev\", \"python3-pip\", \"python3-venv\", \"python3-dev\", \"build-essential\"]' upgrade=True update=True"
    ]
  }
  provisioner "shell" {
    environment_vars = [
      "EDX_ANSIBLE_BRANCH=${var.ansible_branch}",
    ]
    inline = [
      "cd /tmp && git clone https://github.com/edx/configuration --depth 1 --branch $EDX_ANSIBLE_BRANCH",
      "cd /tmp/configuration && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
    ]
  }
  provisioner "ansible-local" {
    playbook_file = "${path.root}/files/edx_${var.node_type}_playbook.yml"
    command = "/tmp/configuration/.venv/bin/ansible-playbook"
    staging_directory = "/tmp/configuration/playbooks/"
  }
  provisioner "shell-local" {
    environment_vars = ["NODE_TYPE=${var.node_type}"]
    inline = ["pyinfra --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-${build.ID}.pem ${build.Host} ${path.root}/deploy.py"]
  }
}
