"""Create the resources needed to run a xqwatcher server.  # noqa: D200"""

# Note: This stack has a silent dependency on an peering connection between the VPC
# that it is installed in and the VPC(s) that contain the xqueue instances.

import base64
import json
import textwrap
from pathlib import Path

import pulumi_vault as vault
import yaml
from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from pulumi import Config, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam

from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    TagSpecification,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
stack_info = parse_stack()
xqwatcher_config = Config("xqwatcher")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

target_vpc_name = xqwatcher_config.get("target_vpc")
target_vpc = network_stack.require_output(target_vpc_name)
vpc_id = target_vpc["id"]

consul_security_groups = consul_stack.require_output("security_groups")
consul_provider = get_consul_provider(stack_info)

vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)

aws_account = get_caller_identity()

aws_config = AWSBase(
    tags={
        "OU": xqwatcher_config.get("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-xqwatcher",
        "Owner": "platform-engineering",
    }
)
xqwatcher_server_tag = f"open-edx-xqwatcher-server-{env_name}"

openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)

xqwatcher_server_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["open-edx-xqwatcher-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
        ec2.GetAmiFilterArgs(name="tag:deployment", values=[stack_info.env_prefix]),
        ec2.GetAmiFilterArgs(name="tag:openedx_release", values=[openedx_release]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

###############################
##     General Resources     ##
###############################

# IAM and instance profile
xqwatcher_server_instance_role = iam.Role(
    f"xqwatcher-server-instance-role-{env_name}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    path="/ol-infrastructure/xqwatcher-server/role/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    f"xqwatcher-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=xqwatcher_server_instance_role.name,
)
xqwatcher_server_instance_profile = iam.InstanceProfile(
    f"xqwatcher-server-instance-profile-{env_name}",
    role=xqwatcher_server_instance_role.name,
    path="/ol-infrastructure/xqwatcher-server/profile/",
)

# Vault policy definition
xqwatcher_server_vault_policy = vault.Policy(
    f"xqwatcher-server-vault-policy-{env_name}",
    name=f"xqwatcher-server-{stack_info.env_prefix}",
    policy=Path(__file__)
    .parent.joinpath("xqwatcher_server_policy.hcl")
    .read_text()
    .replace("DEPLOYMENT", f"{stack_info.env_prefix}"),
)
# Register xqwatcher AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    f"xqwatcher-server-ami-ec2-vault-auth-{env_name}",
    backend=f"aws-{stack_info.env_prefix}",
    auth_type="iam",
    role="xqwatcher-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[xqwatcher_server_instance_profile.arn],
    bound_ami_ids=[xqwatcher_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[xqwatcher_server_vault_policy.name],
)

##################################
#     Network Access Control     #
##################################
# Create security group
xqwatcher_server_security_group = ec2.SecurityGroup(
    f"xqwatcher-server-security-group-{env_name}",
    name=f"xqwatcher-server-operations-{env_name}",
    description="Access control for xqwatcher servers",
    ingress=[],  # no listeners on xqwatcher nodes
    egress=default_egress_args,
    vpc_id=vpc_id,
)

###################################
#     Web Node EC2 Deployment     #
###################################

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

vault_secrets = read_yaml_secrets(
    Path(f"xqwatcher/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
)
xqwatcher_vault_mount_name = vault_mount_stack.require_output("xqwatcher_kv")["path"]
vault.kv.SecretV2(
    f"xqwatcher-{env_name}-grader-static-secrets",
    mount=xqwatcher_vault_mount_name,
    name=f"{stack_info.env_prefix}-grader-config",
    data_json=json.dumps(vault_secrets),
)

block_device_mappings = [BlockDeviceMapping(volume_size=25)]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": xqwatcher_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": xqwatcher_server_tag}),
    ),
]

lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=xqwatcher_server_ami.id,
    instance_type=xqwatcher_config.get("instance_type")
    or InstanceTypes.burstable_small,
    instance_profile_arn=xqwatcher_server_instance_profile.arn,
    security_groups=[
        xqwatcher_server_security_group,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": xqwatcher_server_tag}),
    tag_specifications=tag_specs,
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/consul.d/02-autojoin.json",
                                "content": json.dumps(
                                    {
                                        "retry_join": [
                                            "provider=aws tag_key=consul_env "
                                            f"tag_value={consul_dc}"
                                        ],
                                        "datacenter": consul_dc,
                                    }
                                ),
                                "owner": "consul:consul",
                            },
                            {
                                "path": "/etc/default/vector",
                                "content": textwrap.dedent(
                                    f"""\
                            ENVIRONMENT={consul_dc}
                            APPLICATION=xqwatcher-{stack_info.env_prefix}
                            VECTOR_CONFIG_DIR=/etc/vector/
                            VECTOR_STRICT_ENV_VARS=false
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                            """
                                ),
                                "owner": "root:root",
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

auto_scale_config = xqwatcher_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"xqwatcher-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"] or 2,
    min_size=auto_scale_config["min"] or 1,
    max_size=auto_scale_config["max"] or 3,
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": xqwatcher_server_tag}),
)

as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
)

export("xqwatcher_security_group", xqwatcher_server_security_group.id)
