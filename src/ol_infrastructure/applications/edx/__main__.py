from pulumi import Config, ResourceOptions
from pulumi_aws import ec2

from ol_infrastructure.infrastructure import operations as ops
from ol_infrastructure.lib.ol_types import AWSBase

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

edxapp_security_group = ec2.SecurityGroup(
    f'edxapp-{environment_name}-security-group',
    name=f'edxapp-{environment_name}',
    description='Access control to edxapp',
    tags=aws_config.tags,
    vpc_id=destination_vpc['id'],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=['0.0.0.0/0'],
            protocol='tcp',
            from_port=80,
            to_port=80,
            description='HTTP access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=['0.0.0.0/0'],
            protocol='tcp',
            from_port=443,
            to_port=443,
            description='HTTPS access'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[destination_vpc['cidr']],
            protocol='tcp',
            from_port=18040,
            to_port=18040,
            description='Xqueue access'
        )
    ]
)

edx_worker_security_group = ec2.SecurityGroup(
    f'edx-worker-{environment_name}-security-group',
    name=f'edx-worker-{environment_name}',
    description='Access control to edx-worker',
    tags=aws_config.tags,
    vpc_id=destination_vpc['id'],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            self=True,
            protocol='tcp',
            from_port=18040,
            to_port=18040,
            description='Xqueue access'
        )
    ]
)

ec2.SecurityGroupRule(
    f'edxapp-{environment_name}-to-edx-worker-tcp-access-rule',
    type='ingress',
    from_port=18040,
    to_port=18040,
    protocol='tcp',
    description='Access from edxapp to edx-worker',
    source_security_group_id=edxapp_security_group.id,
    security_group_id=edxapp_security_group.id,
    opts=ResourceOptions(parent=edxapp_security_group)
)

ec2.SecurityGroupRule(
    f'edxapp-{environment_name}-to-elasticsearch-tcp-access-rule',
    type='ingress',
    from_port=9200,
    to_port=9200,
    protocol='tcp',
    description='Access from edxapp to elasticsearch cluster',
    source_security_group_id=ops.elasticsearch.elasticsearch_security_group.id,
    security_group_id=ops.elasticsearch.elasticsearch_security_group.id,
    opts=ResourceOptions(parent=ops.elasticsearch.elasticsearch_security_group)
)

ec2.SecurityGroupRule(
    f'edx-worker-{environment_name}-to-elasticsearch-tcp-access-rule',
    type='ingress',
    from_port=9200,
    to_port=9200,
    protocol='tcp',
    description='Access from edx-worker to elasticsearch cluster',
    source_security_group_id=ops.elasticsearch.elasticsearch_security_group.id,
    security_group_id=ops.elasticsearch.elasticsearch_security_group.id,
    opts=ResourceOptions(parent=ops.elasticsearch.elasticsearch_security_group)
)
