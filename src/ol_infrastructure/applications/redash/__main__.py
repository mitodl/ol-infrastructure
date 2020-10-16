"""The complete state necessary to deploy an instance of the Redash application.

- Create an RDS PostgreSQL instance for storing Redash's configuration data and intermediate query results
- Mount a Vault database backend and provision role definitions for the Redash RDS database
- Create an IAM role for Redash instances to allow access to S3 and other AWS resources
- Create a Redis cluster in Elasticache
- Register a minion ID and key pair with the appropriate SaltStack master instance
- Provision a set of EC2 instances from a pre-built AMI with the configuration and code for Redash
- Provision an AWS load balancer and connect the deployed EC2 instances
- Create a DNS record for the deployed load balancer
"""
import json

from itertools import chain

from pulumi import Config, ResourceOptions, StackReference, export, get_stack
from pulumi.config import get_config
from pulumi_aws import (
    ec2,
    get_ami,
    get_caller_identity,
    iam,
    route53
)

from ol_infrastructure.components.aws.cache import (
    OLAmazonCache,
    OLAmazonRedisConfig
)
from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, build_userdata
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs
)

# TODO:
# - provision Redis cluster
# - create autoscaling group for worker nodes
# - create load balancer for web nodes

redash_config = Config('redash')
salt_config = Config('saltstack')
stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
dns_stack = StackReference('infrastructure.aws.dns')
policy_stack = StackReference('infrastructure.aws.policies')
mitodl_zone_id = dns_stack.require_output('odl_zone_id')
data_vpc = network_stack.require_output('data_vpc')
operations_vpc = network_stack.require_output('operations_vpc')
redash_environment = f'data-{env_suffix}'
aws_config = AWSBase(
    tags={
        'OU': 'data',
        'Environment': redash_environment
    },
)

redash_instance_role = iam.Role(
    'redash-instance-role',
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
    name=f'redash-instance-role-{env_suffix}',
    path='/ol-data/redash-role/',
    tags=aws_config.tags
)

iam.RolePolicyAttachment(
    f'redash-role-policy-{redash_environment}',
    policy_arn=policy_stack.require_output('iam_policies')['describe_instances'],
    role=redash_instance_role.name
)

redash_instance_profile = iam.InstanceProfile(
    f'redash-instance-profile-{env_suffix}',
    role=redash_instance_role.name,
    name=f'redash-instance-profile-{env_suffix}',
    path='/ol-data/redash-profile/'
)

redash_db_security_group = ec2.SecurityGroup(
    f'redash-db-access-{env_suffix}',
    name=f'ol-redash-db-access-{env_suffix}',
    description='Access from the data VPC to the Redash database',
    ingress=[ec2.SecurityGroupIngressArgs(
        cidr_blocks=[data_vpc['cidr'], operations_vpc['cidr']],
        ipv6_cidr_blocks=[data_vpc['cidr_v6']],
        protocol='tcp',
        from_port=5432,  # noqa: WPS432
        to_port=5432  # noqa: WPS432
    )],
    tags=aws_config.tags,
    vpc_id=data_vpc['id']
)

redash_db_config = OLPostgresDBConfig(
    instance_name=f'redash-db-{env_suffix}',
    password=redash_config.require_secret('db_password'),
    subnet_group_name=data_vpc['rds_subnet'],
    security_groups=[redash_db_security_group],
    tags=aws_config.tags,
    db_name='redash',
    **defaults(stack)['rds'],
)
redash_db = OLAmazonDB(redash_db_config)

redash_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=redash_db_config.db_name,
    mount_point=f'{redash_db_config.engine}-redash-{redash_environment}',
    db_admin_username=redash_db_config.username,
    db_admin_password=redash_config.require_secret('db_password'),
    db_host=redash_db.db_instance.address,
)
redash_db_vault_backend = OLVaultDatabaseBackend(redash_db_vault_backend_config)

redis_config = Config('redis')
redash_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require_secret('auth_token'),
    engine_version='6.x',
    node_group_count=3,
    replica_count=3,
    auto_upgrade=True,
    cluster_description='Redis cluster for Redash tasks and caching',
    cluster_name=f'redash-redis-{redash_environment}',
    instance_type=redis_config.require('instance_type'),
    num_instances=3,
    security_groups=[],  # TODO: Create Redis security group and hook it to Redash security group. (TMM 2020-10-16)
    subnet_group=data_vpc['elasticache_subnet'],  # the name of the subnet group created in the OLVPC component resource
)
redash_redis_cluster = OLAmazonCache(redash_redis_config)

instance_type_name = redash_config.get('instance_type') or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
redash_instances = []
redash_export = {}
subnets = data_vpc['subnet_ids']
subnet_id = subnets.apply(chain)
redash_ami = get_ami(
    filters=[
        {
            'name': 'tag:Name',
            'values': ['redash'],
        },
        {
            'name': 'virtualization-type',
            'values': ['hvm'],
        },
    ],
    most_recent=True,
    owners=[str(get_caller_identity().account_id)])
for count, subnet in zip(range(redash_config.get_int('instance_count') or 3), subnets):  # type: ignore # noqa: WPS221
    instance_name = f'redash-{redash_environment}-{count}'
    salt_minion = OLSaltStackMinion(
        f'saltstack-minion-{instance_name}',
        OLSaltStackMinionInputs(
            minion_id=instance_name
        )
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=['redash'],
        minion_environment=redash_environment,
        salt_host=f'salt-{env_suffix}.private.odl.mit.edu')

    instance_tags = aws_config.merged_tags({
        'Name': instance_name,
    })
    redash_instance = ec2.Instance(
        f'redash-instance-{redash_environment}-{count}',
        ami=redash_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=redash_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        key_name=salt_config.require('key_name'),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type='gp2',
            volume_size=20
        ),
        vpc_security_group_ids=[
            data_vpc['security_groups']['default'],
            data_vpc['security_groups']['salt_minion'],
            data_vpc['security_groups']['web'],
        ],
        opts=ResourceOptions(depends_on=[salt_minion])
    )
    redash_instances.append(redash_instance)

    redash_export[instance_name] = {
        'public_ip': redash_instance.public_ip,
        'private_ip': redash_instance.private_ip,
        'ipv6_address': redash_instance.ipv6_addresses
    }

fifteen_minutes = 60 * 15
redash_domain = route53.Record(
    f'redash-{env_suffix}-service-domain',
    name=get_config('redash:domain'),
    type='A',
    ttl=fifteen_minutes,
    records=[instance['public_ip'] for instance in redash_export.values()],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[redash_instance])
)
redash_domain_v6 = route53.Record(
    f'redash-{env_suffix}-service-domain-v6',
    name=get_config('redash:domain'),
    type='AAAA',
    ttl=fifteen_minutes,
    records=[instance['ipv6_address'][0] for instance in redash_export.values()],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[redash_instance])
)

export('redash_app', {
    'rds_host': redash_db.db_instance.address,
    'instances': redash_export
})
