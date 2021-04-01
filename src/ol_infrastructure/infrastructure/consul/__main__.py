import json
from itertools import chain

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2, iam, route53

from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    build_userdata,
    debian_10_ami,
    default_egress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

stack_info = parse_stack()
env_config = Config("environment")
consul_config = Config("consul")
salt_config = Config("saltstack")
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
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
            from_port=8500,
            to_port=8500,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="udp",
            from_port=8500,
            to_port=8500,
            description="HTTP API access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="tcp",
            from_port=8600,
            to_port=8600,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="udp",
            from_port=8600,
            to_port=8600,
            description="DNS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="tcp",
            from_port=8300,
            to_port=8301,
            description="LAN gossip protocol",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc["cidr"]],
            protocol="udp",
            from_port=8300,
            to_port=8301,
            description="LAN gossip protocol",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=peer_vpcs.apply(
                lambda peer_vpcs: [peer["cidr"] for peer in peer_vpcs.values()]
            ),
            protocol="tcp",
            from_port=8300,
            to_port=8302,
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
            from_port=8301,
            to_port=8301,
            self=True,
            description="LAN gossip protocol from servers",
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[consul_server_security_group.id],
            protocol="udp",
            from_port=8301,
            to_port=8301,
            self=True,
            description="LAN gossip protocol from servers",
        ),
    ],
)

security_groups = {
    "consul_server": consul_server_security_group.id,
    "consul_agent": consul_agent_security_group.id,
}

instance_type_name = consul_config.get("instance_type") or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
consul_instances = []
export_data = {}
subnets = destination_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)
salt_environment = Config("saltstack").get("environment_name") or environment_name
for count, subnet in zip(range(consul_config.get_int("instance_count") or 3), subnets):  # type: ignore # noqa: WPS221
    instance_name = f"consul-{environment_name}-{count}"
    salt_minion = OLSaltStackMinion(
        f"saltstack-minion-{instance_name}",
        OLSaltStackMinionInputs(minion_id=instance_name),
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=["consul_server", "service_discovery"],
        minion_environment=salt_environment,
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
        key_name=salt_config.require("key_name"),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type="gp2", volume_size=20
        ),
        vpc_security_group_ids=[
            destination_vpc["security_groups"]["default"],
            destination_vpc["security_groups"]["salt_minion"],
            destination_vpc["security_groups"]["web"],
            consul_server_security_group.id,
        ],
        opts=ResourceOptions(depends_on=[salt_minion]),
    )
    consul_instances.append(consul_instance)

    export_data[instance_name] = {
        "public_ip": consul_instance.public_ip,
        "private_ip": consul_instance.private_ip,
        "ipv6_address": consul_instance.ipv6_addresses,
    }

fifteen_minutes = 60 * 15
consul_domain = route53.Record(
    f"consul-{environment_name}-dns-record",
    name=f"consul-{salt_environment}.odl.mit.edu",
    type="A",
    ttl=fifteen_minutes,
    records=[consul_server.public_ip for consul_server in consul_instances],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=consul_instances),
)

export(
    "security_groups",
    {
        "consul_server": consul_server_security_group.id,
        "consul_agent": consul_agent_security_group.id,
    },
)
export("instances", export_data)
