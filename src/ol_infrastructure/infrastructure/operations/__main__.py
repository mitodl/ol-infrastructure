import consul

from pulumi import Config, StackReference, get_stack
from pulumi_aws import ec2

env_config = Config('environment')
stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
env_prefix = namespace.rsplit('.', 1)[-1]
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
security_stack = StackReference('infrastructure.aws.security')
environment_name = f'{env_prefix}-{env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'
destination_vpc = network_stack.require_output(env_config.require('vpc_reference'))

ec2.SecurityGroupRule(
    type='ingress',
    cidr_blocks=[destination_vpc['cidr']],
    protocol='tcp',
    from_port=8300,
    to_port=8302,
    description='WAN cross-datacenter communication',
    security_group_id=consul.consul_server_security_group.id
)
