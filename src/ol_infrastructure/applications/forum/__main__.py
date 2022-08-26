"""Create the resources needed to run a forum server.
"""
import base64
import json
import textwrap
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import FORUM_SERVICE_PORT
from bridge.secrets.sops import read_yaml_secrets
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

stack_info = parse_stack()
forum_config = Config("forum")
if Config("vault").get("address"):
    setup_vault_provider()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)

vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
edxapp = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = forum_config.get("target_vpc")
openedx_version_tag = forum_config.get("openedx_version_tag")
target_vpc = network_stack.require_output(target_vpc_name)

aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
forum_server_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["open-edx-forum-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
        ec2.GetAmiFilterArgs(name="tag:deployment", values=[stack_info.env_prefix]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)
aws_config = AWSBase(
    tags={
        "OU": forum_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-forum",
        "Owner": "platform-engineering",
    }
)
forum_server_tag = f"open-edx-forum-server-{env_name}"
consul_security_groups = consul_stack.require_output("security_groups")
consul_provider = get_consul_provider(stack_info)

# IAM and instance profile
forum_server_instance_role = iam.Role(
    f"forum-server-instance-role-{env_name}",
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
    path=f"/ol-applications/open-edx-forum/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
# Vault policy definition
forum_vault_policy = vault.Policy(
    "open-edx-forum-server-vault-policy",
    name=f"forum-{stack_info.env_prefix}",
    policy=Path(__file__)
    .parent.joinpath("forum_policy.hcl")
    .read_text()
    .replace("DEPLOYMENT", f"{stack_info.env_prefix}"),
)
# Register edX Platform AMI for Vault AWS auth
aws_vault_backend = f"aws-{stack_info.env_prefix}"
iam.RolePolicyAttachment(
    f"forum-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=forum_server_instance_role.name,
)
forum_server_instance_profile = iam.InstanceProfile(
    f"open-edx-forum-server-instance-profile-{env_name}",
    role=forum_server_instance_role.name,
    path="/ol-infrastructure/open-edx-forum-server/profile/",
)
forum_app_vault_auth_role = vault.aws.AuthBackendRole(
    "forum-web-ami-ec2-vault-auth",
    backend=aws_vault_backend,
    auth_type="iam",
    role="forum-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[forum_server_instance_profile.arn],
    bound_ami_ids=[forum_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[forum_vault_policy.name],
)
consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
block_device_mappings = [BlockDeviceMapping()]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": forum_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": forum_server_tag}),
    ),
]
forum_server_security_group = ec2.SecurityGroup(
    f"forum-server-security-group-{env_name}",
    name=f"forum-server-{target_vpc_name}-{env_name}",
    description="Access control for forum servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=FORUM_SERVICE_PORT,
            to_port=FORUM_SERVICE_PORT,
            security_groups=[edxapp.get_output("edxapp_security_group")],
            description=f"Allow traffic to the forum server on port {FORUM_SERVICE_PORT}",
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)
lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=forum_server_ami.id,
    instance_type=forum_config.get("instance_type") or InstanceTypes.burstable_medium,
    instance_profile_arn=forum_server_instance_profile.arn,
    security_groups=[
        forum_server_security_group,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": forum_server_tag}),
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
                            APPLICATION=forum
                            VECTOR_CONFIG_DIR=/etc/vector/
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                            """
                                ),
                                "owner": "root:root",
                            },
                        ],
                        "bootcmd": [
                            "sed -i -e 's/latest/{openedx_version_tag}/' /etc/docker/compose/docker-compose.yaml"
                        ],
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)
auto_scale_config = forum_config.get_object("auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"open-edx-forum-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"],
    min_size=auto_scale_config["min"],
    max_size=auto_scale_config["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": forum_server_tag}),
)
as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
)
