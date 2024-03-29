import base64
import json
import textwrap
from pathlib import Path

import bcrypt
import yaml
from bridge.lib.magic_numbers import (
    CONSUL_DNS_PORT,
    CONSUL_HTTP_PORT,
    CONSUL_LAN_SERF_PORT,
    CONSUL_RPC_PORT,
    CONSUL_WAN_SERF_PORT,
    DEFAULT_HTTPS_PORT,
    FIVE_MINUTES,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53

from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
env_config = Config("environment")
consul_config = Config("consul")
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
business_unit = env_config.get("business_unit") or "operations"
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
peer_vpcs = destination_vpc["peers"].apply(
    lambda peers: {peer: network_stack.require_output(peer) for peer in peers}
)
aws_config = AWSBase(tags={"OU": business_unit, "Environment": env_name})
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
vpc_id = destination_vpc["id"]
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")

#############
# IAM Setup #
#############

consul_instance_role = iam.Role(
    f"consul-instance-role-{env_name}",
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
    path="/ol-operations/consul/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"consul-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=consul_instance_role.name,
)
iam.RolePolicyAttachment(
    "caddy-route53-records-permission",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=consul_instance_role.name,
)

consul_instance_profile = iam.InstanceProfile(
    f"consul-instance-profile-{env_name}",
    role=consul_instance_role.name,
    path="/ol-operations/consul/profile/",
)


########################
# Security Group Setup #
########################

cidr_blocks = [destination_vpc["cidr"]]

consul_server_security_group = ec2.SecurityGroup(
    f"consul-server-{env_name}-security-group",
    name=f"{env_name}-consul-server",
    description="Access control between Consul severs and agents",
    tags=aws_config.merged_tags({"Name": f"{env_name}-consul-server"}),
    vpc_id=destination_vpc["id"],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="tcp",
            from_port=CONSUL_HTTP_PORT,
            to_port=CONSUL_HTTP_PORT,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="udp",
            from_port=CONSUL_HTTP_PORT,
            to_port=CONSUL_HTTP_PORT,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="tcp",
            from_port=CONSUL_DNS_PORT,
            to_port=CONSUL_DNS_PORT,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="udp",
            from_port=CONSUL_DNS_PORT,
            to_port=CONSUL_DNS_PORT,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="tcp",
            from_port=CONSUL_RPC_PORT,
            to_port=CONSUL_LAN_SERF_PORT,
            description="LAN gossip protocol",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=cidr_blocks,
            protocol="udp",
            from_port=CONSUL_RPC_PORT,
            to_port=CONSUL_LAN_SERF_PORT,
            description="LAN gossip protocol",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=peer_vpcs.apply(
                lambda peer_vpcs: [
                    peer.apply(lambda vpc: vpc["cidr"]) for peer in peer_vpcs.values()
                ]
            ),
            protocol="tcp",
            from_port=CONSUL_RPC_PORT,
            to_port=CONSUL_WAN_SERF_PORT,
            description="WAN cross-datacenter communication",
        ),
    ],
    egress=default_egress_args,
)

consul_agent_security_group = ec2.SecurityGroup(
    f"consul-agent-{env_name}-security-group",
    name=f"{env_name}-consul-agent",
    description="Access control between Consul agents",
    tags=aws_config.merged_tags({"Name": f"{env_name}-consul-agent"}),
    vpc_id=vpc_id,
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[consul_server_security_group.id],
            protocol="tcp",
            from_port=CONSUL_LAN_SERF_PORT,
            to_port=CONSUL_LAN_SERF_PORT,
            self=True,
            description="LAN gossip protocol from servers",
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[consul_server_security_group.id],
            protocol="udp",
            from_port=CONSUL_LAN_SERF_PORT,
            to_port=CONSUL_LAN_SERF_PORT,
            self=True,
            description="LAN gossip protocol from servers",
        ),
    ],
)

security_groups = {
    "consul_server": consul_server_security_group.id,
    "consul_agent": consul_agent_security_group.id,
}


#############
# ALB Setup #
#############

# Assumes that there are at least as many subnets as there is consul instances
consul_capacity = consul_config.get_int("instance_count") or 3
subnet_ids = destination_vpc["subnet_ids"]

# Create Application LB
# Could be converted to an EC2 running HAProxy
consul_elb_tag = f"consul-lb-{env_name}"
consul_elb = lb.LoadBalancer(
    "consul-server-load-balancer",
    name=f"consul-{env_name}",
    load_balancer_type="application",
    ip_address_type="dualstack",
    enable_http2=True,
    subnets=subnet_ids,
    security_groups=[
        destination_vpc["security_groups"]["default"],
        destination_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": consul_elb_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
consul_lb_target_group = lb.TargetGroup(
    "consul-lb-target-group",
    vpc_id=vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=3,
        timeout=3,
        interval=10,
        path="/v1/agent/host",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    name_prefix=f"consul-{env_name}-"[:6],
    tags=aws_config.tags,
)

wildcard_arn_cert = acm.get_certificate(
    domain="*.odl.mit.edu",
    most_recent=True,
    statuses=["ISSUED"],
)

consul_lb_listener = lb.Listener(
    "consul-lb-listener",
    certificate_arn=wildcard_arn_cert.arn,
    load_balancer_arn=consul_elb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    # more than default listener rule needed?
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=consul_lb_target_group.arn,
        )
    ],
    opts=ResourceOptions(delete_before_replace=True),  # Other options needed?
)


##################
# Route 53 Setup #
##################

FIFTEEN_MINUTES = 60 * 15
consul_dns_name = f"consul-{env_name}.odl.mit.edu"
consul_domain = route53.Record(
    "consul-server-dns-record",
    name=consul_dns_name,
    type="CNAME",
    ttl=FIFTEEN_MINUTES,
    records=[consul_elb.dns_name],
    zone_id=mitodl_zone_id,  # should this be consul_elb.zone_id...?
)


#########################
# Launch Template Setup #
#########################

# Find AMI
aws_account = get_caller_identity()
consul_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["consul-server-*"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Select instance type
instance_type_name = (
    consul_config.get("instance_type") or InstanceTypes.burstable_micro.name
)
instance_type = InstanceTypes[instance_type_name].value

# Auto Join WAN Envs
# using the VPC ID to denote datacenter
retry_join_wan = peer_vpcs.apply(
    lambda vpc_dict: [
        peer.apply(lambda vpc: f"provider=aws tag_key=consul_vpc tag_value={vpc['id']}")
        for peer in vpc_dict.values()
    ]
)


# Make cloud-init userdata
def cloud_init_userdata(
    consul_vpc_id,  # noqa: ARG001
    consul_env_name,
    retry_join_wan_array,
    domain_name,
    basic_auth_password,
):
    hashed_password = bcrypt.hashpw(
        basic_auth_password.encode("utf8"), bcrypt.gensalt()
    )
    grafana_credentials = read_yaml_secrets(
        Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
    )
    cloud_config_contents = {
        "write_files": [
            {
                "path": "/etc/consul.d/99-autojoin.json",
                "content": json.dumps(
                    {
                        "retry_join": [
                            "provider=aws tag_key=consul_env "
                            f"tag_value={consul_env_name}"
                        ],
                        "datacenter": consul_env_name,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/consul.d/99-autojoin-wan.json",
                "content": json.dumps(
                    {
                        "retry_join_wan": retry_join_wan_array,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/default/traefik",
                "content": f"DOMAIN={domain_name}\n",
            },
            {
                "path": "/etc/traefik/.htpasswd",
                "content": f"pulumi:{hashed_password.decode('utf-8')}\n",
                "owner": "traefik:traefik",
                "permissions": "0600",
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(
                    f"""\
                    ENVIRONMENT={consul_env_name}
                    APPLICATION=consul
                    SERVICE=consul
                    VECTOR_CONFIG_DIR=/etc/vector/
                    VECTOR_STRICT_ENV_VARS=false
                    GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                    """
                ),
                "owner": "root:root",
            },
        ]
    }

    return base64.b64encode(
        "#cloud-config\n{}".format(
            yaml.dump(
                cloud_config_contents,
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")


consul_launch_config = ec2.LaunchTemplate(
    "consul-launch-template",
    name_prefix=f"consul-{env_name}-",
    description="Launch template for deploying Consul cluster",
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=consul_config.get_int("storage_disk_capacity") or 100,
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
                encrypted=True,
                kms_key_id=kms_ebs["arn"],
            ),
        ),
    ],
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=consul_instance_profile.arn,
    ),
    image_id=consul_ami.id,
    instance_type=instance_type,
    key_name="oldevops",
    tags=aws_config.tags,
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags(
                {
                    "Name": f"consul-{env_name}",
                    "consul_env": env_name,
                    "consul_vpc": vpc_id,
                }
            ),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": f"consul-{env_name}"}),
        ),
    ],
    user_data=Output.all(
        vpc_id=vpc_id,
        retry_join_wan=retry_join_wan,
        pulumi_password=Output.secret(
            read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
                "basic_auth_password"
            ]
        ),
    ).apply(
        lambda init_dict: cloud_init_userdata(
            init_dict["vpc_id"],
            env_name,
            init_dict["retry_join_wan"],
            consul_dns_name,
            init_dict["pulumi_password"],
        )
    ),
    vpc_security_group_ids=[
        destination_vpc["security_groups"]["web"],
        security_groups["consul_server"],
        security_groups["consul_agent"],
    ],
)


#########################
# Autoscale Group Setup #
#########################


consul_asg = autoscaling.Group(
    f"consul-{env_name}-autoscaling-group",
    desired_capacity=consul_capacity,
    max_size=consul_capacity,
    min_size=consul_capacity,
    health_check_type="EC2",
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=consul_launch_config.id,
        version="$Latest",
    ),
    target_group_arns=[consul_lb_target_group.arn],
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=85,
            instance_warmup=FIVE_MINUTES,
        ),
        triggers=["tag"],
    ),
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": consul_ami.id}
        ).items()
    ],
    vpc_zone_identifiers=subnet_ids,
)

#################
# Stack Exports #
#################

export("security_groups", security_groups)
export("consul_lb", {"dns_name": consul_elb.dns_name, "arn": consul_elb.arn})
export("consul_launch_config", consul_launch_config.id)
export("consul_asg", consul_asg.id)
export("datacenter", env_name)
