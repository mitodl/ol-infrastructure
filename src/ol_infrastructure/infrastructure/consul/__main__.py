import json
from itertools import chain

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import autoscaling, ec2, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import (
    CONSUL_DNS_PORT,
    CONSUL_HTTP_PORT,
    CONSUL_LAN_SERF_PORT,
    CONSUL_RPC_PORT,
    CONSUL_WAN_SERF_PORT,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    availability_zones,
    build_userdata,
    debian_10_ami,
    default_egress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack


stack_info = parse_stack()  # needed without salt?
env_config = Config("environment")
consul_config = Config("consul")
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"  # without salt format?
business_unit = env_config.get("business_unit") or "operations"
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
peer_vpcs = destination_vpc["peers"].apply(
    lambda peers: {peer: network_stack.require_output(peer) for peer in peers}
)
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

consul_instance_role = iam.Role(
    f"consul-instance-role-{environment_name}",
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
    f"consul-describe-instance-role-policy-{environment_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=consul_instance_role.name,
)

consul_instance_profile = iam.InstanceProfile(
    f"consul-instance-profile-{environment_name}",
    role=consul_instance_role.name,
    path="/ol-operations/consul/profile/",
)

consul_server_security_group = ec2.SecurityGroup(
    f"consul-server-{environment_name}-security-group",
    name=f"{environment_name}-consul-server",
    description="Access control between Consul severs and agents",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-consul-server"}),
    vpc_id=destination_vpc["id"],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="tcp",
            from_port=CONSUL_HTTP_PORT,
            to_port=CONSUL_HTTP_PORT,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="udp",
            from_port=CONSUL_HTTP_PORT,
            to_port=CONSUL_HTTP_PORT,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="tcp",
            from_port=CONSUL_DNS_PORT,
            to_port=CONSUL_DNS_PORT,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="udp",
            from_port=CONSUL_DNS_PORT,
            to_port=CONSUL_DNS_PORT,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="tcp",
            from_port=CONSUL_RPC_PORT,
            to_port=CONSUL_LAN_SERF_PORT,
            description="LAN gossip protocol",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
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
    f"consul-agent-{environment_name}-security-group",
    name=f"{environment_name}-consul-agent",
    description="Access control between Consul agents",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-consul-agent"}),
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

# This section will need to be replaced with Launch Template config
instance_type_name = consul_config.get("instance_type") or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
consul_instances = []
export_data = {}
subnets = destination_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)
instance_range = range(consul_config.get_int("instance_count") or 3)
for count, subnet in zip(instance_range, subnets):  # type:ignore
    subnet_object = ec2.get_subnet(id=subnet)
    # This is only necessary for the operations environment because the 1i AZ is lacking
    # support for newer instance types. We need to retire that subnet to avoid further
    # hacks like this one.

    # TODO: redeploy or otherwise migrate instances out of the 1e AZ and delete the
    # associated subnet. (TMM 2021-05-07)
    if subnet_object.availability_zone == "us-east-1e":
        continue
    instance_name = f"consul-{environment_name}-{count}"

    # Will cloud init be needed for pyinfra, rather than salt, setup?
    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        # minion_keys=salt_minion,
        minion_roles=["consul_server", "service_discovery"],
        # minion_environment=salt_environment,
        salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
    )

    instance_tags = aws_config.merged_tags(
        {"Name": instance_name, "consul_env": environment_name}
    )
    consul_instance = ec2.Instance(
        f"consul-instance-{environment_name}-{count}",
        ami=debian_10_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=consul_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        # key_name=salt_config.require("key_name"),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type=DiskTypes.ssd, volume_size=20
        ),
        vpc_security_group_ids=[
            destination_vpc["security_groups"]["default"],
            # destination_vpc["security_groups"]["salt_minion"],
            destination_vpc["security_groups"]["web"],
            consul_server_security_group.id,
        ],
        # opts=ResourceOptions(depends_on=[salt_minion]),
    )
    consul_instances.append(consul_instance)

    export_data[instance_name] = {
        "public_ip": consul_instance.public_ip,
        "private_ip": consul_instance.private_ip,
        "ipv6_address": consul_instance.ipv6_addresses,
    }
######

fifteen_minutes = 60 * 15
consul_domain = route53.Record(
    f"consul-{environment_name}-dns-record",
    # name=f"consul-{salt_environment}.odl.mit.edu",
    type="A",
    ttl=fifteen_minutes,
    records=[consul_server.public_ip for consul_server in consul_instances],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=consul_instances),
)

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

consul_launch_config = ec2.LaunchTemplate(
    "consul-launch-template",
    name_prefix=f"consul-{environment_name}-",
    description="Launch template for deploying Consul cluster",
    iam_instance_profile=consul_instance_profile.id,
    image_id=consul_ami.id,
    instance_type=InstanceTypes[instance_type_name].value,
    # key_name=salt_config.require("key_name"),
    tags=instance_tags,
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": f"consul-{stack_info.env_suffix}"}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": f"consul-{stack_info.env_suffix}"}),
        ),
    ],
    vpc_security_group_ids=[
        # destination_vpc["security_groups"]["salt_minion"],
        destination_vpc["security_groups"]["web"],
        consul_server_security_group.id,
    ],
)

consul_asg = autoscaling.Group(
    f"consul-{environment_name}-autoscaling-group",
    availability_zones=availability_zones,
    desired_capacity=3,
    max_size=5,
    min_size=3,
    health_check_type="EC2",  # consider custom health check to verify consul health
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
)

export(
    "security_groups",
    {
        "consul_server": consul_server_security_group.id,
        "consul_agent": consul_agent_security_group.id,
    },
)
export("instances", export_data)
export("consul_launch_config", consul_launch_config.id)
export("consul_asg", consul_asg.id)
