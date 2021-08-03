"""Deploy a cluster of Vault servers using autoscale groups and KMS auto-unseal.

- Creates an instance policy granting access to IAM for use with the AWS secrets
  backend, and granting permissions to a KMS key for auto-unseal.

- Creates an autoscale group that launches a pre-built AMI with Vault installed.

- Creates a load balancer and attaches it to the ASG with an internal Route53 entry
  for simplifying discovery of the Vault cluster.

- Uses the cloud auto-join functionality to automate new instances joining the
  cluster.  The requisite configuration is passed in via cloud-init user data.
"""

# TODO: Mount separate disk for Raft data to simplify snapshot backup/restore
import json

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    VAULT_CLUSTER_PORT,
    VAULT_HTTP_PORT,
)
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes, InstanceTypes
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

vault_config = Config("vault")
stack_info = parse_stack()
target_vpc = vault_config.require("target_vpc")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
target_vpc = network_stack.require_output(target_vpc)
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": vault_config.get("business_unit") or "operations",
        "Environment": env_name,
        "Owner": "platform-engineering",
    }
)
aws_account = get_caller_identity()
vault_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["vault-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)
kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")

# write file

#######################
# Access and Security #
#######################

# IAM Policy and role
vault_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:DescribeKey",
            ],
            "Resource": f"arn:aws:kms:us-east-1:{aws_account}:alias/vault-auto-unseal-{stack_info.env_suffix}",  # noqa: E501
        },
        {
            "Effect": "Allow",
            "Action": [
                "iam:AttachUserPolicy",
                "iam:CreateAccessKey",
                "iam:CreateUser",
                "iam:DeleteAccessKey",
                "iam:DeleteUser",
                "iam:DeleteUserPolicy",
                "iam:DetachUserPolicy",
                "iam:ListAccessKeys",
                "iam:ListAttachedUserPolicies",
                "iam:ListGroupsForUser",
                "iam:ListUserPolicies",
                "iam:PutUserPolicy",
                "iam:AddUserToGroup",
                "iam:RemoveUserFromGroup",
            ],
            "Resource": [f"arn:aws:iam::{aws_account}:user/vault-*"],
        },
    ],
}

vault_policy = iam.Policy(
    "vault-policy",
    name_prefix="vault-server-policy-",
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        vault_policy_document,
        stringify=True,
    ),
    description="AWS access permissions for Vault server instances",
)
vault_iam_role = iam.Role(
    "vault-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name_prefix=f"{env_name}-vault-server-role-",
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    "vault-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=vault_iam_role.name,
)
iam.RolePolicyAttachment(
    "vault-role-policy",
    policy_arn=vault_policy.arn,
    role=vault_iam_role.name,
)
vault_instance_profile = iam.InstanceProfile(
    f"vault-server-instance-profile-{env_name}",
    name_prefix=f"{env_name}-vault-server-",
    role=vault_iam_role.name,
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
)

# Security Group
vault_security_group = ec2.SecurityGroup(
    "vault-server-security-group",
    name_prefix="vault-server-{env_name}-",
    description="Network access controls for traffic to and from Vault servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=VAULT_HTTP_PORT,
            to_port=VAULT_CLUSTER_PORT,
            self=True,
            description="Allow traffic between Vault server nodes in a cluster",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=VAULT_HTTP_PORT,
            to_port=VAULT_HTTP_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description="Allow traffic to Vault server API endpoints",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
    vpc_id=target_vpc["id"],
)

#################
# Load Balancer #
#################
# Create load balancer for Edxapp web nodes
web_lb = lb.LoadBalancer(
    "vault-web-load-balancer",
    name_prefix=f"vault-server-{env_name}-",
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=target_vpc["subnet_ids"],
    security_groups=[
        target_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
lms_web_lb_target_group = lb.TargetGroup(
    "vault-web-lms-alb-target-group",
    vpc_id=target_vpc["id"],
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=3,
        timeout=3,
        interval=10,
        path="/v1/sys/health",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
        matcher="200,429",
    ),
    name_prefix=f"lms-{stack_info.env_suffix}-"[:6],
    tags=aws_config.tags,
)
odl_wildcard_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
vault_web_alb_listener = lb.Listener(
    "vault-web-alb-listener",
    certificate_arn=odl_wildcard_cert.certificate_arn,
    load_balancer_arn=web_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=lms_web_lb_target_group.arn,
        )
    ],
    opts=ResourceOptions(delete_before_replace=True),
)

###################
# Autoscale Group #
###################
# TODO: Encrypt EBS
vault_instance_type = (
    vault_config.get("worker_instance_type") or InstanceTypes.general_purpose_large.name
)
worker_launch_config = ec2.LaunchTemplate(
    "vault-server-launch-template",
    name_prefix=f"vault-{env_name}-",
    description="Launch template for deploying Vault server nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=vault_instance_profile.arn,
    ),
    image_id=vault_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=25,  # noqa: WPS432
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
                encrypted=True,
                kms_key_id=kms_ebs["id"],
            ),
        ),
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvdb",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=vault_config.get("storage_disk_capacity")
                or 100,  # noqa: WPS432
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
                encrypted=True,
                kms_key_id=kms_ebs["id"],
            ),
        ),
    ],
    vpc_security_group_ids=[
        vault_security_group.id,
        target_vpc["security_groups"]["web"],
    ],
    instance_type=InstanceTypes[vault_instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
        ),
    ],
    tags=aws_config.tags,
)
worker_asg = autoscaling.Group(
    "vault-worker-autoscaling-group",
    desired_capacity=vault_config.get_int("worker_node_capacity") or 1,
    min_size=1,
    max_size=50,  # noqa: WPS432
    health_check_type="EC2",
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=worker_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50  # noqa: WPS432
        ),
        triggers=["tag"],
    ),
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.tags.items()
    ],
)

#################
# Stack Exports #
#################
export(
    "vault_server",
    {
        "security_group": vault_security_group.id,
    },
)
