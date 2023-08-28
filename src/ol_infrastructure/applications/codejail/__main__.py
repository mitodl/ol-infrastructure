"""Create the resources needed to run a codejail service.  # noqa: D200
"""
import base64
import json
import textwrap
from pathlib import Path

import yaml
from pulumi import Config, StackReference
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import CODEJAIL_SERVICE_PORT
from bridge.secrets import sops
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
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

stack_info = parse_stack()
codejail_config = Config("codejail")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)

edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = codejail_config.get("target_vpc")
target_vpc = network_stack.require_output(target_vpc_name)

aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)
codejail_server_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["open-edx-codejail-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
        ec2.GetAmiFilterArgs(name="tag:deployment", values=[stack_info.env_prefix]),
        ec2.GetAmiFilterArgs(name="tag:openedx_release", values=[openedx_release]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)
aws_config = AWSBase(
    tags={
        "OU": codejail_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-codejail",
        "Owner": "platform-engineering",
    }
)
codejail_server_tag = f"open-edx-codejail-server-{env_name}"
consul_security_groups = consul_stack.require_output("security_groups")
consul_provider = get_consul_provider(stack_info)

# IAM and instance profile
codejail_server_instance_role = iam.Role(
    f"codejail-server-instance-role-{env_name}",
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
    path=f"/ol-applications/open-edx-codejail/{stack_info.env_prefix}/{stack_info.env_suffix}/",  # noqa: E501
    tags=aws_config.tags,
)
# Register edX Platform AMI for Vault AWS auth
aws_vault_backend = f"aws-{stack_info.env_prefix}"
iam.RolePolicyAttachment(
    f"codejail-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=codejail_server_instance_role.name,
)
codejail_server_instance_profile = iam.InstanceProfile(
    f"open-edx-codejail-server-instance-profile-{env_name}",
    role=codejail_server_instance_role.name,
    path="/ol-infrastructure/open-edx-codejail-server/profile/",
)
consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = sops.read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
block_device_mappings = [BlockDeviceMapping()]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": codejail_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": codejail_server_tag}),
    ),
]
codejail_server_security_group = ec2.SecurityGroup(
    f"codejail-server-security-group-{env_name}",
    name=f"codejail-server-{target_vpc_name}-{env_name}",
    description="Access control for codejail servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=CODEJAIL_SERVICE_PORT,
            to_port=CODEJAIL_SERVICE_PORT,
            security_groups=[edxapp_stack.get_output("edxapp_security_group")],
            description=f"Allow traffic to the codejail server on port {CODEJAIL_SERVICE_PORT}",  # noqa: E501
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)
lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=codejail_server_ami.id,
    instance_type=codejail_config.get("instance_type") or InstanceTypes.burstable_micro,
    instance_profile_arn=codejail_server_instance_profile.arn,
    security_groups=[
        codejail_server_security_group,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": codejail_server_tag}),
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
                            APPLICATION=edx
                            SERVICE=codejail
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
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)
auto_scale_config = codejail_config.get_object("auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"open-edx-codejail-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"],
    min_size=auto_scale_config["min"],
    max_size=auto_scale_config["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": codejail_server_tag}),
)
as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
)
