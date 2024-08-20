"""Create the resources needed to run a tika server.  # noqa: D200"""

import base64
import json
import textwrap
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference
from pulumi_aws import ec2, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
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
tika_config = Config("tika")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.apps.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

target_vpc_name = tika_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)

consul_security_groups = consul_stack.require_output("security_groups")
aws_config = AWSBase(
    tags={
        "OU": tika_config.get("business_unit") or "operations",
        "Environment": f"{env_name}",
    }
)
aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
tika_server_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["tika-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

tika_server_tag = f"tika-server-{env_name}"
consul_provider = get_consul_provider(stack_info)

###############################
##     General Resources     ##
###############################

# IAM and instance profile
tika_server_instance_role = iam.Role(
    f"tika-server-instance-role-{env_name}",
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
    path="/ol-infrastructure/tika-server/role/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    f"tika-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=tika_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"tika-server-route53-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=tika_server_instance_role.name,
)
tika_server_instance_profile = iam.InstanceProfile(
    f"tika-server-instance-profile-{env_name}",
    role=tika_server_instance_role.name,
    path="/ol-infrastructure/tika-server/profile/",
)

# Vault policy definition
tika_server_vault_policy = vault.Policy(
    "tika-server-vault-policy",
    name="tika-server",
    policy=Path(__file__).parent.joinpath("tika_server_policy.hcl").read_text(),
)
# Register Tika AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "tika-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="tika-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[tika_server_instance_profile.arn],
    bound_ami_ids=[tika_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[tika_server_vault_policy.name],
)

##################################
#     Network Access Control     #
##################################
# Create security group
tika_server_security_group = ec2.SecurityGroup(
    f"tika-server-security-group-{env_name}",
    name=f"tika-server-operations-{env_name}",
    description="Access control for tika servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the tika server on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)

###################################
#     Web Node EC2 Deployment     #
###################################
lb_config = OLLoadBalancerConfig(
    subnets=target_vpc["subnet_ids"],
    security_groups=[tika_server_security_group],
    tags=aws_config.merged_tags({"Name": tika_server_tag}),
)

tg_config = OLTargetGroupConfig(
    vpc_id=vpc_id,
    health_check_path="/version",
    tags=aws_config.merged_tags({"Name": tika_server_tag}),
)

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
x_access_token = read_yaml_secrets(Path(f"tika/tika.{stack_info.env_suffix}.yaml"))[
    "x_access_token"
]

# Store the access token in vault
vault.generic.Secret(
    "tika-server-x-access-token-vault-secret",
    path="secret-operations/tika/access-token",
    data_json=json.dumps({"value": x_access_token}),
)

block_device_mappings = [BlockDeviceMapping()]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": tika_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": tika_server_tag}),
    ),
]

lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=tika_server_ami.id,
    instance_type=tika_config.get("instance_type")
    or InstanceTypes.general_purpose_2xlarge,
    instance_profile_arn=tika_server_instance_profile.arn,
    security_groups=[
        tika_server_security_group,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": tika_server_tag}),
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
                            APPLICATION=tika
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
                            {
                                "path": "/etc/docker/compose/.env",
                                "content": textwrap.dedent(
                                    f"""\
                            DOMAIN={tika_config.require("web_host_domain")}
                            X_ACCESS_TOKEN={x_access_token}
                            """
                                ),
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

auto_scale_config = tika_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"tika-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"] or 2,
    min_size=auto_scale_config["min"] or 1,
    max_size=auto_scale_config["max"] or 3,
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": tika_server_tag}),
)

as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
    tg_config=tg_config,
    lb_config=lb_config,
)

## Create Route53 DNS records for tika nodes
five_minutes = 60 * 5
route53.Record(
    "tika-server-dns-record",
    name=tika_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
