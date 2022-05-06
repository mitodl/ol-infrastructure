"""Create the resources needed to run a tika server.

"""
import base64
import json
import textwrap
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
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
            description=f"Allow traffic to the tika server on port {DEFAULT_HTTPS_PORT}",
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)

###################################
#     Web Node EC2 Deployment     #
###################################

# Create load balancer for Concourse web nodes
LOAD_BALANCER_NAME_MAX_LENGTH = 32
tika_server_lb = lb.LoadBalancer(
    "tika-server-load-balancer",
    name=f"tika-server-alb-{stack_info.env_prefix[:3]}-{stack_info.env_suffix[:2]}"[
        :LOAD_BALANCER_NAME_MAX_LENGTH
    ],
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=target_vpc["subnet_ids"],
    security_groups=[
        tika_server_security_group.id,
    ],
    tags=aws_config.merged_tags({"Name": tika_server_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
tika_server_lb_target_group = lb.TargetGroup(
    "tika-server-alb-target-group",
    vpc_id=vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=120,
        path="/version",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
        matcher="200",
    ),
    name=tika_server_tag[:TARGET_GROUP_NAME_MAX_LENGTH],
    tags=aws_config.tags,
)
tika_server_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
tika_server_alb_listener = lb.Listener(
    "tika-server-alb-listener",
    certificate_arn=tika_server_acm_cert.arn,
    load_balancer_arn=tika_server_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=tika_server_lb_target_group.arn,
        )
    ],
)

## Create auto scale group and launch configs for a tika server
instance_type = tika_config.get("instance_type") or InstanceTypes.burstable_medium.name
consul_datacenter = consul_stack.require_output("datacenter")

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

x_access_token = read_yaml_secrets(Path(f"tika/tika.{stack_info.env_suffix}.yaml"))[
    "x_access_token"
]

tika_server_launch_config = ec2.LaunchTemplate(
    "tika-server-launch-template",
    name_prefix=f"tika-server-{env_name}-",
    description="Launch template for deploying tika servers",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=tika_server_instance_profile.arn,
    ),
    image_id=tika_server_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=tika_config.get_int("disk_size") or 25,
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
            ),
        )
    ],
    vpc_security_group_ids=[
        tika_server_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": tika_server_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": tika_server_tag}),
        ),
    ],
    tags=aws_config.tags,
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
                            VECTOR_CONFIG_DIR=/etc/vector/
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                            """
                                ),  # noqa: WPS355
                                "owner": "root:root",
                            },
                            {
                                "path": "/etc/default/caddy",
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

autoscaling.Group(
    "tika-server-autoscaling-group",
    desired_capacity=2,
    min_size=1,
    max_size=3,
    health_check_type="ELB",
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=tika_server_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50
        ),
        triggers=["tags"],
    ),
    target_group_arns=[tika_server_lb_target_group.arn],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": tika_server_ami.id}
        ).items()
    ],
)


## Create Route53 DNS records for tika nodes
five_minutes = 60 * 5
route53.Record(
    "tika-server-dns-record",
    name=tika_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[tika_server_lb.dns_name],
    zone_id=mitodl_zone_id,
)
