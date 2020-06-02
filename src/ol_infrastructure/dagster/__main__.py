import pulumi
import boto3
from pulumi_aws import rds, s3, ec2, iam
from pulumi_vault import database

# Create bucket for Dagster state
# Create RDS Postgres DB for Dagster persistence
# Create IAM role for Dagster instances
# - access to Dagster S3 bucket
# - write access to destination S3 buckets
# Create EC2 instance from Packer AMI
# Create/attach security groups for Dagster
# Vault roles for AWS credentials and RDS credentials
# Vault profile for Dagster to read necessary configurations

minimum_tags = {
    'OU': 'operations',
    'business_unit': 'operations',
    'environment': 'operations'
}

dagster_bucket = 'dagster-production'

dagster_policy = {
    'Version': '2012-10-17',
    'Statement': [
        {
            "Effect": "Allow",
            "Action": [
                "s3:List*",
                "s3:Get*",
                "s3:Put*"
            ],
            "Resource": [
                f"arn:aws:s3:::{dagster_bucket}",
                f"arn:aws:s3:::{dagster_bucket}/*"
            ]
        }
    ]
}

dagster_runtime_bucket = s3.Bucket(dagster_bucket, acl='private', tags=minimum_tags, versioning={'enabled': True})

etl_output_bucket = s3.Bucket()

rds_boto_client = boto3.client('rds', region='us-east-1')
engine_details = rds_boto_client.describe_db_engine_versions(Engine='postgres')

# Create instance profile for granting access to S3 buckets
dagster_iam_policy = iam.Policy(
    'dagster_policy',
    policy=dagster_policy,
    description='Policy for granting acces for Dagster data workflow engine to AWS resources')

dagster_role = iam.Role('dagster_instance_role', assume_role_policy=dagster_iam_policy)

iam.InstanceProfile('dagster_instance_profile', role=dagster_role)

postgres_parameter_group = rds.ParameterGroup()


dagster_db = rds.Instance(
    'dagster-storage',
    engine='postgres',
    engine_version='12.2',
    allocated_storage=50,
    max_allocated_storage=250,
    auto_minor_version_upgrade=True,
    backup_retention_period=30,
    character_set_name='utf8',
    copy_tags_to_snapshot=True,
    deletion_protection=True,
    instance_class
)

