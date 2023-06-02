"""Create the infrastructure and services needed to support the bootcamps application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""
import json

import pulumi_vault as vault
from pulumi import Config, StackReference, export
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
bootcamps_config = Config("bootcamps")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={
        "OU": "bootcamps",
        "Environment": f"applications_{stack_info.env_suffix}",
        "Application": "bootcamps",
    }
)

# Create S3 buckets

# Bucket used to store file uploads from bootcamps app.
bootcamps_storage_bucket_name = f"ol-bootcamps-app-{stack_info.env_suffix}"
bootcamps_storage_bucket = s3.Bucket(
    f"ol-bootcamps-app-{stack_info.env_suffix}",
    bucket=bootcamps_storage_bucket_name,
    versioning=s3.BucketVersioningArgs(
        enabled=True,
    ),
    policy=json.dumps(
        {
            "Version": "2008-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bootcamps_storage_bucket_name}/courses/*",  # noqa: E501
                }
            ],
        }
    ),
    tags=aws_config.tags,
)


bootcamps_iam_policy = iam.Policy(
    f"bootcamps-{stack_info.env_suffix}-policy",
    description="AWS access controls for the bootcamps application in the "
    f"{stack_info.name} environment",
    path=f"/ol-applications/bootcamps/{stack_info.env_suffix}/",
    name_prefix="aws-permissions-",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListAllMyBuckets",
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:*MultiPartUpload*",
                        "s3:ListBucket*",
                        "s3:PutObject*",
                        "s3:GetObject*",
                        "s3:DeleteObject*",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{bootcamps_storage_bucket_name}",
                        f"arn:aws:s3:::{bootcamps_storage_bucket_name}/*",
                    ],
                },
            ],
        },
        stringify=True,
        parliament_config={
            "PERMISSIONS_MANAGEMENT_ACTIONS": {
                "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
            },
            "RESOURCE_EFFECTIVELY_STAR": {},
        },
    ),
)

bootcamps_vault_backend_role = vault.aws.SecretBackendRole(
    f"bootcamps-app-{stack_info.env_suffix}",
    name=f"bootcamps-app-{stack_info.env_suffix}",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[bootcamps_iam_policy.arn],
)

# Create RDS instance
bootcamps_db_security_group = ec2.SecurityGroup(
    f"bootcamps-db-access-{stack_info.env_suffix}",
    description=f"Access control for the bootcamps DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            security_groups=[data_vpc["security_groups"]["integrator"]],
        ),
    ],
    tags=aws_config.merged_tags(
        {"name": f"bootcamps-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

bootcamps_db_config = OLPostgresDBConfig(
    instance_name=f"bootcamps-db-applications-{stack_info.env_suffix}",
    password=bootcamps_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[bootcamps_db_security_group],
    tags=aws_config.tags,
    db_name="bootcamps",
    public_access=True,
    **defaults(stack_info)["rds"],
)
bootcamps_db = OLAmazonDB(bootcamps_db_config)

bootcamps_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=bootcamps_db_config.db_name,
    mount_point=f"{bootcamps_db_config.engine}-bootcamps",
    db_admin_username=bootcamps_db_config.username,
    db_admin_password=bootcamps_db_config.password.get_secret_value(),
    db_host=bootcamps_db.db_instance.address,
    **defaults(stack_info)["rds"],
)
bootcamps_vault_backend = OLVaultDatabaseBackend(bootcamps_vault_backend_config)

######################
# Secrets Management #
######################
bootcamps_secrets = vault.Mount(
    "bootcamps-vault-secrets-storage",
    path="secret-bootcamps",
    type="generic",
    description="Static secrets storage for the bootcamps application",
)

export(
    "bootcamps_app",
    {
        "rds_host": bootcamps_db.db_instance.address,
    },
)
