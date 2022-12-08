# Create the resources needed to run an edxnotes server

import base64
import json
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    TagSpecification,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
notes_config = Config("edxnotes")
if Config("vault").get("address"):
    setup_vault_provider()
consul_provider = get_consul_provider(stack_info)

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = notes_config.get("target_vpc")
openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)
notes_server_tag = f"open-edx-notes-server-{env_name}"
target_vpc = network_stack.require_output(target_vpc_name)

dns_zone = dns_stack.require_output(notes_config.require("dns_zone"))
dns_zone_id = dns_zone["id"]

secrets = read_yaml_secrets(Path(f"edx_notes/{env_name}.yaml"))

aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
notes_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="tag:OU", values=[f"{stack_info.env_prefix}"]),
        ec2.GetAmiFilterArgs(name="name", values=["edx_notes-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
        ec2.GetAmiFilterArgs(name="tag:openedx_release", values=[openedx_release]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

aws_config = AWSBase(
    tags={
        "OU": notes_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-notes",
        "Owner": "platform-engineering",
        "openedx_release": openedx_release,
    }
)

notes_instance_role = iam.Role(
    f"edx-notes-instance-role-{env_name}",
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
    path=f"/ol-applications/open-edx-notes/{stack_info.env_prefix}/{stack_info.env_suffix}/",  # noqa: E501
    tags=aws_config.tags,
)

notes_vault_policy = vault.Policy(
    "edx-notes-vault-policy",
    name=f"edx-notes-{stack_info.env_prefix}",
    policy=Path(__file__)
    .parent.joinpath("edx_notes_policy.hcl")
    .read_text()
    .replace("DEPLOYMENT", f"{stack_info.env_prefix}"),
)
aws_vault_backend = f"aws-{stack_info.env_prefix}"
iam.RolePolicyAttachment(
    f"edx-notes-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=notes_instance_role.name,
)
iam.RolePolicyAttachment(
    "edx-notes-traefik-route53-records-permission",
    policy_arn=policy_stack.require_output("iam_policies")[
        f"route53_{notes_config.require('dns_zone')}_zone_records"  # noqa: WPS237
    ],
    role=notes_instance_role.name,
)

notes_instance_profile = iam.InstanceProfile(
    f"edx-notes-instance-profile-{env_name}",
    role=notes_instance_role.name,
    path="/ol-infrastructure/open-edx-notes-server/profile/",
)
notes_vault_auth_role = vault.aws.AuthBackendRole(
    "notes-ami-ec2-vault-auth",
    backend=aws_vault_backend,
    role="edx-notes-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[notes_instance_profile.arn],
    bound_ami_ids=[notes_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[notes_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

notes_server_secrets = vault.generic.Secret(
    "notes-server-configuration-secrets",
    path=f"secret-{stack_info.env_prefix}/edx-notes",
    data_json=json.dumps(secrets),
)

consul_datacenter = consul_stack.require_output("datacenter")

notes_security_group = ec2.SecurityGroup(
    f"notes-server-security-group-{env_name}",
    name=f"notes-server-{target_vpc_name}-{env_name}",
    description="Access control for notes severs.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=f"Allow traffic to the notes server on port {DEFAULT_HTTPS_PORT}",  # noqa: E501
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
    tags=aws_config.merged_tags({"Name": notes_server_tag}),
)
lb_config = OLLoadBalancerConfig(
    listener_user_acm=True,
    listener_cert_domain=notes_config.require("acm_cert_domain"),
    subnets=target_vpc["subnet_ids"],
    security_groups=[notes_security_group],
    tags=aws_config.merged_tags({"Name": notes_server_tag}),
)

tg_config = OLTargetGroupConfig(
    vpc_id=vpc_id,
    health_check_interval=60,
    health_check_matcher="404",  # TODO 20221208 MAD need to revisit health checks + traefik
    health_check_path="/",
    tags=aws_config.merged_tags({"Name": notes_server_tag}),
)

consul_datacenter = consul_stack.require_output("datacenter")
block_device_mappings = [BlockDeviceMapping()]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    ),
]

lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=notes_ami.id,
    instance_type=notes_config.get("instance_type") or InstanceTypes.burstable_medium,
    instance_profile_arn=notes_instance_profile.arn,
    security_groups=[
        notes_security_group,
        consul_stack.require_output("security_groups")["consul_agent"],
    ],
    tag_specifications=tag_specs,
    tags=aws_config.merged_tags({"Name": notes_server_tag}),
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
                        ],
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

auto_scale_config = notes_config.get_object("auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"notes-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"],
    min_size=auto_scale_config["min"],
    max_size=auto_scale_config["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": notes_server_tag}),
)
as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
    tg_config=tg_config,
    lb_config=lb_config,
)

dns_name = notes_config.get("domain")

consul_keys = {
    "edx/release": openedx_release,
    "edx/notes-api-host": dns_name,
    "edx/deployment": f"{stack_info.env_prefix}",
    "elasticsearch/host": "elasticsearch.service.consul",
}
consul.Keys(
    "notes-server-configuration-data",
    keys=consul_key_helper(consul_keys),
    opts=consul_provider,
)

five_minutes = 60 * 5

route53.Record(
    f"notes-server-dns-records-{dns_name}",
    name=dns_name,
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=dns_zone_id,
)
