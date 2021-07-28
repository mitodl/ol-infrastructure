import base64
import json

import yaml
from pulumi import Config, StackReference, export
from pulumi_aws import autoscaling, ec2, get_caller_identity, iam, lb, route53

from bridge.lib.magic_numbers import (
    CONSUL_DNS_PORT,
    CONSUL_HTTP_PORT,
    CONSUL_LAN_SERF_PORT,
    CONSUL_RPC_PORT,
    CONSUL_WAN_SERF_PORT,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    availability_zones,
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
                lambda peer_vpcs: [peer["cidr"] for peer in peer_vpcs.values()]
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
    vpc_id=destination_vpc["id"],
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
# NLB Setup #
#############

# Assumes that there are at least as many subnets as there is consul instances
consul_capacity = consul_config.get_int("instance_count") or 3
instance_range = range(consul_capacity)
subnet_ids = destination_vpc["subnet_ids"]

# Create Application LB
# Could be converted to an EC2 running HAProxy
consul_elb_tag = f"consul-lb-{env_name}"
consul_elb = lb.LoadBalancer(
    name="consul-lb",
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

# TODO ELB Health Check + Listeners


##################
# Route 53 Setup #
##################

FIFTEEN_MINUTES = 60 * 15
consul_domain = route53.Record(
    name=f"consul-{env_name}.odl.mit.edu",
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
        ec2.GetAmiFilterArgs(name="name", values=["consul"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Select instance type
instance_type_name = consul_config.get("instance_type") or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value

# Auto Join WAN Envs
# using the VPC ID to denote datacenter
datacenter_name = destination_vpc["id"]
wan_envs = [peer["id"] for peer in peer_vpcs]
retry_join_wan = [
    f"provider=aws tag_key=consul_env tag_value={wan_env}" for wan_env in wan_envs
]

# Make cloud-init userdata
cloud_init_user_data = base64.b64encode(
    "#cloud-config\n{}".format(
        yaml.dump(
            {
                "write_files": [
                    {
                        "path": "/etc/consul.d/99-autojoin.json",
                        "content": json.dumps(
                            {
                                "retry_join": [
                                    "provider=aws tag_key=consul_env "
                                    f"tag_value={env_name}"
                                ],
                                "datacenter": datacenter_name,
                            }
                        ),
                        "owner": "consul:consul",
                    },
                    {
                        "path": "/etc/consul.d/99-autojoin-wan.json.json",
                        "content": json.dumps(
                            {
                                "retry_join_wan": retry_join_wan,
                            }
                        ),
                        "owner": "consul:consul",
                    },
                ]
            },
            sort_keys=True,
        )
    ).encode("utf8")
).decode("utf8")

consul_launch_config = ec2.LaunchTemplate(
    "consul-launch-template",
    name_prefix=f"consul-{env_name}-",
    description="Launch template for deploying Consul cluster",
    # block_device_mappings
    # TODO: additional block device mappings needed here or to be defined in AMI?
    iam_instance_profile=consul_instance_profile.id,
    image_id=consul_ami.id,
    instance_type=instance_type,
    key_name="oldevops",
    tags=aws_config.tags,
    # TODO: tag volumes via LaunchTemplate, or in AMI?
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": f"consul-{env_name}"}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": f"consul-{env_name}"}),
        ),
    ],
    user_data=cloud_init_user_data,
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
    availability_zones=availability_zones,
    desired_capacity=consul_capacity,
    max_size=consul_capacity,
    min_size=consul_capacity,
    health_check_type="ELB",  # TODO ELB health check
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=consul_launch_config.id,
        version="$Latest",
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
    ),
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.tags.items()
    ],
    vpc_zone_identifiers=subnet_ids,
)

# Attach ASG to LB
asg_attachment_consul = autoscaling.Attachment(
    "asgAttachmentConsul",  # more precise / different name?
    autoscaling_group_name=consul_asg.id,
    elb=consul_elb.id,
)

#################
# Stack Exports #
#################

export("security_groups", security_groups)
export("consul_lb", {"dns_name": consul_elb.dns_name, "arn": consul_elb.arn})
export("consul_launch_config", consul_launch_config.id)
export("consul_asg", consul_asg.id)
