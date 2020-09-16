import consul

from pulumi import Config, export

from ol_infrastructure.infrastructure import operations as ops

env_config = Config('environment')
environment_name = f'{ops.env_prefix}-{ops.env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'

export('security_groups', {
    'consul_server': consul.consul_server_security_group.id,
    'consul_agent': consul.consul_agent_security_group.id
})
export('consul', consul.consul_export)
