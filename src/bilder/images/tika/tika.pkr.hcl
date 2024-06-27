locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
  app_name  = "tika"
}

variable "build_environment" {
  type    = string
  default = "operations-ci"
}

variable "business_unit" {
  type    = string
  default = "operations"
}

variable "node_type" {
  type    = string
  default = "server"
}

source "amazon-ebs" "tika" {
  ami_description         = "Deployment image for tika application generated at ${local.timestamp}"
  ami_name                = "tika-${var.node_type}-${local.timestamp}"
  ami_virtualization_type = "hvm"
  instance_type           = "t3a.medium"
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
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
    purpose = "${local.app_name}-${var.node_type}"
  }
  snapshot_tags = {
    Name    = "${local.app_name}-${var.node_type}-ami"
    OU      = "${var.business_unit}"
    app     = "${local.app_name}"
    purpose = "${local.app_name}-${var.node_type}"
  }
  # Base all builds off of the most recent docker_baseline_ami built by us, based of Debian 12

  source_ami_filter {
    filters = {
      name                = "docker_baseline_ami-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["610119931565"]
  }

  ssh_username  = "admin"
  ssh_interface = "public_ip"
  subnet_filter {
    filters = {
      "tag:Environment" : var.build_environment
    }
    random = true
  }
  tags = {
    Name    = "${local.app_name}-${var.node_type}"
    OU      = var.business_unit
    app     = local.app_name
    purpose = "${local.app_name}-${var.node_type}"
  }
}

build {
  sources = [
    "source.amazon-ebs.tika",
  ]
  # Setup the ssh key locally
  provisioner "shell-local" {
    inline = [
      "echo '${build.SSHPrivateKey}' > /tmp/packer-session-${build.ID}.pem",
      "chmod 600 /tmp/packer-session-${build.ID}.pem"
    ]
  }
  # Run the pyinfra to build the AMI
  provisioner "shell-local" {
    environment_vars = [
      "NODE_TYPE=${var.node_type}",
    ]
    inline = ["pyinfra --data ssh_strict_host_key_checking=off --sudo --user ${build.User} --port ${build.Port} --key /tmp/packer-session-${build.ID}.pem ${build.Host} --chdir ${path.root} deploy.py"]
  }

  # Copy the tags json down locally
  provisioner "shell-local" {
    inline = ["scp -o StrictHostKeyChecking=no -i /tmp/packer-session-${build.ID}.pem ${build.User}@${build.Host}:/etc/ami_tags.json /tmp/ami_tags-${build.ID}.json"]
  }

  # Ref: https://developer.hashicorp.com/packer/docs/post-processors/manifest#example-configuration
  post-processor "manifest" {
    output = "/tmp/packer-build-manifest-${build.ID}.json"
  }

  post-processor "shell-local" {
    inline = ["AMI_ID=$(jq -r '.builds[-1].artifact_id' /tmp/packer-build-manifest-${build.ID}.json | cut -d \":\" -f2)",
      "aws ec2 create-tags --resource $AMI_ID --cli-input-json \"$(cat /tmp/ami_tags-${build.ID}.json)\"",
      "export AWS_DEFAULT_REGION=us-east-1",
    "aws --no-cli-pager ec2 describe-images --image-ids $AMI_ID"]
  }
}
