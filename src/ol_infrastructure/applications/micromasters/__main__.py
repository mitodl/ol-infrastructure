"""Create the infrastructure and services needed to support the
MicroMasters application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import pulumi_vault as vault
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from pulumi import Config, StackReference, export
from pulumi_aws import ec2, iam, s3

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
micromasters_config = Config("micromasters")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
micromasters_vpc = network_stack.require_output("applications_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
micromasters_environment = f"micromasters-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": "micromasters",
        "Environment": micromasters_environment,
        "Application": "micromasters",
    }
)

# Create S3 bucket

# Bucket used to store files from MicroMasters app.
micromasters_bucket_name = f"ol-micromasters-app-{stack_info.env_suffix}"
micromasters_audit_bucket_name = f"odl-micromasters-audit-{stack_info.env_suffix}"
micromasters_bucket = s3.Bucket(
    f"micromasters-{stack_info.env_suffix}",
    bucket=micromasters_bucket_name,
    versioning=s3.BucketVersioningArgs(
        enabled=True,
    ),
    tags=aws_config.tags,
    acl="private",
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)


micromasters_iam_policy = iam.Policy(
    f"micromasters-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the MicroMasters application in the "
        f"{stack_info.name} environment"
    ),
    path=f"/ol-applications/micromasters/{stack_info.env_suffix}/",
    name_prefix=f"micromasters-{stack_info.env_suffix}-application-policy-",
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
                        "s3:ListBucket*",
                        "s3:PutObject",
                        "s3:PutObjectAcl",
                        "s3:GetObject*",
                        "s3:DeleteObject*",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{micromasters_bucket_name}",
                        f"arn:aws:s3:::{micromasters_bucket_name}/*",
                        f"arn:aws:s3:::{micromasters_audit_bucket_name}",
                        f"arn:aws:s3:::{micromasters_audit_bucket_name}/*",
                    ],
                },
            ],
        },
        stringify=True,
        parliament_config={
            "PERMISSIONS_MANAGEMENT_ACTIONS": {
                "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
            }
        },
    ),
)

micromasters_vault_backend_role = vault.aws.SecretBackendRole(
    "micromasters-app",
    name="micromasters",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[micromasters_iam_policy.arn],
)

# Create RDS instance
micromasters_db_security_group = ec2.SecurityGroup(
    f"micromasters-db-access-{stack_info.env_suffix}",
    description=f"Access control for the MicroMasters App DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku",
        )
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.merged_tags(
        {"Name": "micromasters-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=micromasters_vpc["id"],
)

micromasters_db_config = OLPostgresDBConfig(
    instance_name=f"micromasters-{stack_info.env_suffix}-app-db",
    password=micromasters_config.require("db_password"),
    subnet_group_name=micromasters_vpc["rds_subnet"],
    security_groups=[micromasters_db_security_group],
    tags=aws_config.tags,
    db_name="micromasters",
    public_access=True,
    **defaults(stack_info)["rds"],
)
micromasters_db = OLAmazonDB(micromasters_db_config)

micromasters_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=micromasters_db_config.db_name,
    mount_point=f"{micromasters_db_config.engine}-micromasters",
    db_admin_username=micromasters_db_config.username,
    db_admin_password=micromasters_db_config.password.get_secret_value(),
    db_host=micromasters_db.db_instance.address,
)
micromasters_vault_backend = OLVaultDatabaseBackend(micromasters_vault_backend_config)

export("micromasters_app", {"rds_host": micromasters_db.db_instance.address})
