import consul

from pulumi import Config, export
from pulumi_aws import ec2

from ol_infrastructure.infrastructure import operations as ops

env_config = Config('environment')
environment_name = f'{ops.env_prefix}-{ops.env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'
destination_vpc = ops.network_stack.require_output(env_config.require('vpc_reference'))

ec2.SecurityGroupRule(
    'consul-server-cross-dc-access-{ops.namespace}',
    type='ingress',
    cidr_blocks=[destination_vpc['cidr']],
    protocol='tcp',
    from_port=8300,
    to_port=8302,
    description='WAN cross-datacenter communication',
    security_group_id=consul.consul_server_security_group.id
)

export('security_groups', {
    'consul_server': consul.consul_server_security_group.id,
    'consul_agent': consul.consul_agent_security_group.id
})
export('consul', consul.consul_export)
