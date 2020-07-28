from pulumi import StackReference, get_stack
from pulumi.config import get_config
from pulumi_aws import ec2, iam, s3

from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig
)
from ol_infrastructure.components.aws.olvpc import OLVPC
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.stack_defaults import defaults

# Create bucket for Dagster state
# Create RDS Postgres DB for Dagster persistence
# Create IAM role for Dagster instances
# - access to Dagster S3 bucket
# - write access to destination S3 buckets
# Create EC2 instance from Packer AMI
# Create/attach security groups for Dagster
# Vault roles for AWS credentials and RDS credentials
# Vault profile for Dagster to read necessary configurations

stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f'aws.network.{stack_name}')
data_vpc_id = network_stack.get_output('data_vpc_id')
data_vpc_cidr = network_stack.get_output('data_vpc_cidr')
data_vpc_cidr_v6 = network_stack.get_output('data_vpc_cidr_v6')
data_vpc_rds_subnet = network_stack.get_output('data_vpc_rds_subnet')
aws_config = AWSBase(
    tags={
        'OU': 'data',
        'Environment': f'data-{env_suffix}'
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
    ingress=[
        {
            'cidr_blocks': [data_vpc_cidr],
            'ipv6_cidr_blocks': [data_vpc_cidr_v6],
            'protocol': 'tcp',
            'from_port': 5432,
            'to_port': 5432
        }
    ],
    tags=aws_config.tags,
    vpc_id=data_vpc_id
)

dagster_db_config = OLPostgresDBConfig(
    instance_name=f'ol-etl-db-{env_suffix}',
    password=get_config('dagster:db_password'),
    subnet_group_name=data_vpc_rds_subnet,
    security_groups=[dagster_db_security_group],
    tags=aws_config.tags,
    db_name='dagster',
    **defaults(stack)['rds'],
)
dagster_db = OLAmazonDB(dagster_db_config)
