import yaml

from pulumi import Config, StackReference, get_stack
from pulumi_aws import ec2, iam

from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, debian_10_ami
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.providers.salt.minion import (
    OLSaltStack,
    OLSaltStackInputs
)

env_config = Config('environment')
consul_config = Config('consul')
salt_config = Config('saltstack')
stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
env_prefix = namespace.rsplit('.', 1)[-1]
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
security_stack = StackReference('infrastructure.aws.security')
environment_name = f'{env_prefix}-{env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'
aws_config = AWSBase(tags={
    'OU': business_unit,
    'Environment': environment_name
})
destination_vpc = network_stack.require_output(env_config.require('vpc_reference'))

consul_instance_role = iam.Role(
    'consul-instance-role-{environment_name}',
    assume_role_policy={
        'Version': '2012-10-17',
        'Statement': {
            'Effect': 'Allow',
            'Action': 'sts:AssumeRole',
            'Principal': {'Service': 'ec2.amazonaws.com'}
        }
    },
    name=f'consul-instance-role-{environment_name}',
    path='/ol-operations/consul-role/',
    tags=aws_config.tags
)

iam.RolePolicyAttachment(
    f'consul-role-policy-{environment_name}',
    policy_arn=security_stack.require_output('iam_policies')['describe_instances'],
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
    type="ingress",
    from_port=8301,
    to_port=8301,
    protocol="tcp",
    description='LAN gossip protocol from agents',
    security_groups=[consul_agent_security_group.id],
    security_group_id=consul_agent_security_group.id
)

ec2.SecurityGroupRule(
    f'consul-agent-{environment_name}-inter-agent-udp-access-rule',
    type="ingress",
    from_port=8301,
    to_port=8301,
    protocol="udp",
    description='LAN gossip protocol from agents',
    security_groups=[consul_agent_security_group.id],
    security_group_id=consul_agent_security_group.id
)

instance_type_name = consul_config.get('instance_type') or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
consul_instances = []
for count in range(consul_config.get_int('instance_count') or 3):  # noqa: WPS426
    instance_name = f'consul-{environment_name}-{count}'
    salt_minion = OLSaltStack(
        f'saltstack-minion-{instance_name}',
        OLSaltStackInputs(
            minion_id=instance_name,
            salt_api_url=salt_config.require('api_url'),
            salt_user=salt_config.require('api_user'),
            salt_password=salt_config.require_secret('api_password')
        )
    )

    cloud_init_userdata = {
        'salt_minion': {
            'pkg_name': 'salt-minion',
            'service_name': 'salt-minion',
            'config_dir': '/etc/salt',
            'conf': {
                'id': instance_name,
                'master': f'salt-{env_suffix}.private.odl.mit.edu',
                'startup_states': 'highstate'
            },
            'grains': {
                'roles': ['consul_server', 'service_discovery'],
                'context': 'pulumi',
                'environment': environment_name
            }
        }
    }
    salt_minion.minion_public_key.apply(
        lambda pub: cloud_init_userdata['salt_minion'].update({'public_key': pub}))
    salt_minion.minion_private_key.apply(
        lambda priv: cloud_init_userdata['salt_minion'].update({'private_key': priv}))

    instance_tags = aws_config.merged_tags({'Name': instance_name})
    subnets = destination_vpc['subnet_ids']
    consul_instance = ec2.Instance(
        f'dagster-instance-{environment_name}',
        ami=debian_10_ami.id,
        user_data=f'#cloud-config\n{yaml.dump(cloud_init_userdata, sort_keys=True)}',
        instance_type=instance_type,
        iam_instance_profile=consul_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnets[count % len(subnets)],  # type: ignore
        key_name=salt_config.require('key_name'),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type='gp2',
            volume_size=20
        ),
        vpc_security_group_ids=[
            destination_vpc['security_groups']['default'],
            destination_vpc['security_groups']['web'],
            destination_vpc['security_groups']['salt_minion'],
        ]
    )
    consul_instances.append(consul_instance)
