"""The complete state necessary to deploy an instance of the Dagster application.

- Create an S3 bucket for managing Dagster's intermediate states
- Create an RDS PostgreSQL instance for storing Dagster's schedule and run state
- Mount a Vault database backend and provision role definitions for the Dagster RDS database
- Create an IAM role for Dagster instances to allow access to S3 and other AWS resources
- Register a minion ID and key pair with the appropriate SaltStack master instance
- Provision an EC2 instance from a pre-built AMI with the pipeline code for Dagster
"""
import yaml

from pulumi import StackReference, export, get_stack
from pulumi.config import get_config
from pulumi_aws import ec2, get_ami, get_caller_identity, iam, route53, s3

from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.providers.salt.minion import (
    OLSaltStack,
    OLSaltStackInputs
)

stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
dns_stack = StackReference('infrastructure.aws.dns')
mitodl_zone_id = dns_stack.get_output('odl_zone_id')
data_vpc = network_stack.get_output('data_vpc')
operations_vpc = network_stack.get_output('operations_vpc')
dagster_environment = f'data-{env_suffix}'
aws_config = AWSBase(
    tags={
        'OU': 'data',
        'Environment': dagster_environment
    },
)

dagster_bucket_name = f'dagster-{env_suffix}'

dagster_bucket_policy = {
    'Version': '2012-10-17',
    'Statement': [
        {
            'Effect': 'Allow',
            'Action': [
                's3:List*',
                's3:Get*',
                's3:Put*'
            ],
            'Resource': [
                f'arn:aws:s3:::{dagster_bucket_name}',
                f'arn:aws:s3:::{dagster_bucket_name}/*'
            ]
        }
    ]
}

dagster_runtime_bucket = s3.Bucket(
    dagster_bucket_name,
    bucket=dagster_bucket_name,
    acl='private',
    tags=aws_config.tags,
    versioning={'enabled': True},
    server_side_encryption_configuration={
        'rule': {
            'applyServerSideEncryptionByDefault': {
                'sseAlgorithm': 'aws:kms',
            },
        },
    })

# Create instance profile for granting access to S3 buckets
dagster_iam_policy = iam.Policy(
    f'dagster-policy-{env_suffix}',
    name=f'dagster-policy-{env_suffix}',
    path=f'/ol-data/etl-policy-{env_suffix}/',
    policy=dagster_bucket_policy,
    description='Policy for granting acces for batch data workflows to AWS resources'
)

dagster_role = iam.Role(
    'etl-instance-role',
    assume_role_policy={
        'Version': '2012-10-17',
        'Statement': {
            'Effect': 'Allow',
            'Action': 'sts:AssumeRole',
            'Principal': {'Service': 'ec2.amazonaws.com'}
        }
    },
    name=f'etl-instance-role-{env_suffix}',
    path='/ol-data/etl-role/',
    tags=aws_config.tags
)

iam.RolePolicyAttachment(
    f'dagster-role-policy-{env_suffix}',
    policy_arn=dagster_iam_policy.arn,
    role=dagster_role.name
)

dagster_profile = iam.InstanceProfile(
    f'dagster-instance-profile-{env_suffix}',
    role=dagster_role.name,
    name=f'etl-instance-profile-{env_suffix}',
    path='/ol-data/etl-profile/'
)

dagster_db_security_group = ec2.SecurityGroup(
    f'dagster-db-access-{env_suffix}',
    name=f'ol-etl-db-access-{env_suffix}',
    description='Access from the data VPC to the Dagster database',
    ingress=[ec2.SecurityGroupIngressArgs(
        cidr_blocks=[data_vpc['cidr'], operations_vpc['cidr']],
        ipv6_cidr_blocks=[data_vpc['cidr_v6']],
        protocol='tcp',
        from_port=5432,
        to_port=5432
    )],
    tags=aws_config.tags,
    vpc_id=data_vpc['id']
)

dagster_db_config = OLPostgresDBConfig(
    instance_name=f'ol-etl-db-{env_suffix}',
    password=get_config('dagster:db_password'),
    subnet_group_name=data_vpc['rds_subnet'],
    security_groups=[dagster_db_security_group],
    tags=aws_config.tags,
    db_name='dagster',
    **defaults(stack)['rds'],
)
dagster_db = OLAmazonDB(dagster_db_config)

dagster_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=dagster_db_config.db_name,
    mount_point=f'{dagster_db_config.engine}-dagster-{dagster_environment}',
    db_admin_username=dagster_db_config.username,
    db_admin_password=get_config('dagster:db_password'),
    db_host=dagster_db.db_instance.address,
)
dagster_db_vault_backend = OLVaultDatabaseBackend(dagster_db_vault_backend_config)

dagster_minion_id = f'dagster-{dagster_environment}-0'
salt_minion = OLSaltStack(
    f'saltstack-minion-{dagster_minion_id}',
    OLSaltStackInputs(
        minion_id=dagster_minion_id,
        salt_api_url=get_config('saltstack:api_url'),
        salt_user=get_config('saltstack:api_user'),
        salt_password=get_config('saltstack:api_password')
    )
)

cloud_init_userdata = {
    'salt_minion': {
        'pkg_name': 'salt-minion',
        'service_name': 'salt-minion',
        'config_dir': '/etc/salt',
        'conf': {
            'id': dagster_minion_id,
            'master': f'salt-{env_suffix}.private.odl.mit.edu',
            'startup_states': 'highstate'
        },
        'grains': {
            'roles': ['dagster'],
            'context': 'pulumi',
            'environment': dagster_environment
        }
    }
}
salt_minion.minion_public_key.apply(
    lambda pub: cloud_init_userdata['salt_minion'].update({'public_key': pub}))
salt_minion.minion_private_key.apply(
    lambda priv: cloud_init_userdata['salt_minion'].update({'private_key': priv}))

dagster_image = get_ami(
    filters=[
        {
            'name': 'tag:Name',
            'values': ['dagster'],
        },
        {
            'name': 'virtualization-type',
            'values': ['hvm'],
        },
    ],
    most_recent=True,
    owners=[str(get_caller_identity().account_id)])
instance_tags = aws_config.merged_tags({'Name': dagster_minion_id})
dagster_instance = ec2.Instance(
    f'dagster-instance-{dagster_environment}',
    ami=dagster_image.id,
    user_data=f'#cloud-config\n{yaml.dump(cloud_init_userdata, sort_keys=True)}',
    instance_type=get_config('dagster:instance_type'),
    iam_instance_profile=dagster_profile.id,
    tags=instance_tags,
    volume_tags=instance_tags,
    subnet_id=data_vpc['subnet_ids'][0],
    key_name=get_config('saltstack:key_name'),
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_type='gp2',
        volume_size=100
    ),
    vpc_security_group_ids=[
        data_vpc['security_groups']['default'],
        data_vpc['security_groups']['web'],
        data_vpc['security_groups']['salt_minion'],
    ]
)

fifteen_minutes = 60 * 15
dagster_domain = route53.Record(
    f'dagster-{env_suffix}-service-domain',
    name=get_config('dagster:domain'),
    type='A',
    ttl=fifteen_minutes,
    records=[dagster_instance.public_ip],
    zone_id=mitodl_zone_id
)
dagster_domain_v6 = route53.Record(
    f'dagster-{env_suffix}-service-domain-v6',
    name=get_config('dagster:domain'),
    type='AAAA',
    ttl=fifteen_minutes,
    records=dagster_instance.ipv6_addresses,
    zone_id=mitodl_zone_id
)

export('dagster_app', {
    'rds_host': dagster_db.db_instance.address,
    'ec2_private_address': dagster_instance.private_ip,
    'ec2_public_address': dagster_instance.public_ip,
    'ec2_address_v6': dagster_instance.ipv6_addresses
})
