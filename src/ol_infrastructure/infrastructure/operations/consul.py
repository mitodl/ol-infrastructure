import json

from itertools import chain

from pulumi import Config, ResourceOptions
from pulumi_aws import ec2, iam

from ol_infrastructure.infrastructure import operations as ops
from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    build_userdata,
    debian_10_ami
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs
)

env_config = Config('environment')
consul_config = Config('consul')
salt_config = Config('saltstack')
environment_name = f'{ops.env_prefix}-{ops.env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'
aws_config = AWSBase(tags={
    'OU': business_unit,
    'Environment': environment_name
})
destination_vpc = ops.network_stack.require_output(env_config.require('vpc_reference'))

consul_instance_role = iam.Role(
    f'consul-instance-role-{environment_name}',
    assume_role_policy=json.dumps(
        {
            'Version': '2012-10-17',
            'Statement': {
                'Effect': 'Allow',
                'Action': 'sts:AssumeRole',
                'Principal': {'Service': 'ec2.amazonaws.com'}
            }
        }
    ),
    name=f'consul-instance-role-{environment_name}',
    path='/ol-operations/consul-role/',
    tags=aws_config.tags
)

iam.RolePolicyAttachment(
    f'consul-role-policy-{environment_name}',
    policy_arn=ops.policy_stack.require_output('iam_policies')['describe_instances'],
    role=consul_instance_role.name
)

consul_instance_profile = iam.InstanceProfile(
    f'consul-instance-profile-{environment_name}',
    role=consul_instance_role.name,
    name=f'consul-instance-profile-{environment_name}',
    path='/ol-operations/consul-profile/'
)

consul_server_security_group = ec2.SecurityGroup(
    f'consul-server-{environment_name}-security-group',
    name=f'{environment_name}-consul-server',
    description='Access control between Consul severs and agents',
    tags=aws_config.merged_tags({'Name': f'{environment_name}-consul-server'}),
    vpc_id=destination_vpc['id'],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='tcp',
            from_port=8500,
            to_port=8500,
            description='HTTP API access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='udp',
            from_port=8500,
            to_port=8500,
            description='HTTP API access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='tcp',
            from_port=8600,
            to_port=8600,
            description='DNS access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='udp',
            from_port=8600,
            to_port=8600,
            description='DNS access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='tcp',
            from_port=8300,
            to_port=8301,
            description='LAN gossip protocol'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='udp',
            from_port=8300,
            to_port=8301,
            description='LAN gossip protocol'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=ops.peer_vpcs.apply(
                lambda peer_vpcs: [peer['cidr'] for peer in peer_vpcs.values()]),
            protocol='tcp',
            from_port=8300,
            to_port=8302,
            description='WAN cross-datacenter communication'
        )
    ]
)

consul_agent_security_group = ec2.SecurityGroup(
    f'consul-agent-{environment_name}-security-group',
    name=f'{environment_name}-consul-agent',
    description='Access control between Consul agents',
    tags=aws_config.merged_tags({'Name': f'{environment_name}-consul-agent'}),
    vpc_id=destination_vpc['id'],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[consul_server_security_group.id],
            protocol='tcp',
            from_port=8301,
            to_port=8301,
            description='LAN gossip protocol from servers'
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[consul_server_security_group.id],
            protocol='udp',
            from_port=8301,
            to_port=8301,
            description='LAN gossip protocol from servers'
        )
    ]
)

ec2.SecurityGroupRule(
    f'consul-agent-{environment_name}-inter-agent-tcp-access-rule',
    type='ingress',
    from_port=8301,
    to_port=8301,
    protocol='tcp',
    description='LAN gossip protocol from agents',
    source_security_group_id=consul_agent_security_group.id,
    security_group_id=consul_agent_security_group.id,
    opts=ResourceOptions(parent=consul_agent_security_group)
)

ec2.SecurityGroupRule(
    f'consul-agent-{environment_name}-inter-agent-udp-access-rule',
    type='ingress',
    from_port=8301,
    to_port=8301,
    protocol='udp',
    description='LAN gossip protocol from agents',
    source_security_group_id=consul_agent_security_group.id,
    security_group_id=consul_agent_security_group.id,
    opts=ResourceOptions(parent=consul_agent_security_group)
)

instance_type_name = consul_config.get('instance_type') or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
consul_instances = []
consul_export = {}
subnets = destination_vpc['subnet_ids']
subnet_id = subnets.apply(chain)
for count, subnet in zip(range(consul_config.get_int('instance_count') or 3), subnets):  # type: ignore # noqa: WPS221
    instance_name = f'consul-{environment_name}-{count}'
    salt_minion = OLSaltStackMinion(
        f'saltstack-minion-{instance_name}',
        OLSaltStackMinionInputs(
            minion_id=instance_name
        )
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=['consul_server', 'service_discovery'],
        minion_environment=environment_name,
        salt_host=f'salt-{ops.env_suffix}.private.odl.mit.edu')

    instance_tags = aws_config.merged_tags({
        'Name': instance_name,
        'consul_env': environment_name
    })
    consul_instance = ec2.Instance(
        f'consul-instance-{environment_name}-{count}',
        ami=debian_10_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=consul_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        key_name=salt_config.require('key_name'),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type='gp2',
            volume_size=20
        ),
        vpc_security_group_ids=[
            destination_vpc['security_groups']['default'],
            destination_vpc['security_groups']['salt_minion'],
            consul_server_security_group.id
        ],
        opts=ResourceOptions(depends_on=[salt_minion])
    )
    consul_instances.append(consul_instance)

    consul_export[instance_name] = {
        'public_ip': consul_instance.public_ip,
        'private_ip': consul_instance.private_ip,
        'ipv6_address': consul_instance.ipv6_addresses
    }
