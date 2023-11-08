"""Create the infrastructure and services needed to support the MIT XPro application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
from pathlib import Path

import pulumi_vault as vault
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, ResourceOptions, StackReference, export
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

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
xpro_config = Config("xpro")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={
        "OU": "mitxpro",
        "Environment": f"applications_{stack_info.env_suffix}",
        "Application": "xpro",
    }
)

# Create S3 buckets

# Bucket used to store file uploads from xpro app.
xpro_storage_bucket_name = f"ol-xpro-app-{stack_info.env_suffix}"
xpro_storage_bucket = s3.BucketV2(
    f"ol-xpro-app-{stack_info.env_suffix}",
    bucket=xpro_storage_bucket_name,
    tags=aws_config.tags,
)
xpro_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-xpro-app-bucket-ownership-controls",
    bucket=xpro_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioningV2(
    "ol-xpro-app-bucket-versioning",
    bucket=xpro_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
        status="Enabled"
    ),
)
ocw_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-xpro-app-bucket-public-access",
    bucket=xpro_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

xpro_iam_policy = iam.Policy(
    f"xpro-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the xpro application in the "
        f"{stack_info.name} environment"
    ),
    path=f"/ol-applications/xpro/{stack_info.env_suffix}/",
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
                        f"arn:aws:s3:::{xpro_storage_bucket_name}",
                        f"arn:aws:s3:::{xpro_storage_bucket_name}/*",
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

xpro_vault_backend_role = vault.aws.SecretBackendRole(
    "xpro-app",
    name="xpro-app",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[xpro_iam_policy.arn],
)

# Create RDS instance
xpro_db_security_group = ec2.SecurityGroup(
    f"xpro-db-access-{stack_info.env_suffix}",
    description=f"Access control for the xpro DB in {stack_info.name}",
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
        {"name": f"xpro-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

xpro_db_config = OLPostgresDBConfig(
    instance_name=f"xpro-db-applications-{stack_info.env_suffix}",
    password=xpro_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[xpro_db_security_group],
    tags=aws_config.tags,
    db_name="xpro",
    public_access=True,
    **defaults(stack_info)["rds"],
)
xpro_db = OLAmazonDB(xpro_db_config)

xpro_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=xpro_db_config.db_name,
    mount_point=f"{xpro_db_config.engine}-xpro",
    db_admin_username=xpro_db_config.username,
    db_admin_password=xpro_db_config.password.get_secret_value(),
    db_host=xpro_db.db_instance.address,
    **defaults(stack_info)["rds"],
)
xpro_vault_backend = OLVaultDatabaseBackend(xpro_vault_backend_config)

######################
# Secrets Management #
######################
xpro_secrets = vault.Mount(
    "xpro-vault-secrets-storage",
    path="secret-xpro",
    type="kv",
    options={"version": 2},
    description="Static secrets storage for the xpro application",
    opts=ResourceOptions(delete_before_replace=True),
)

xpro_vault_secrets = read_yaml_secrets(
    Path(f"xpro/secrets.{stack_info.env_suffix}.yaml")
)

for key, data in xpro_vault_secrets.items():
    vault.kv.SecretV2(
        f"xpro-vault-secrets-{key}",
        name=key,
        mount=xpro_secrets,
        data_json=json.dumps(data),
    )

export(
    "xpro_app",
    {
        "rds_host": xpro_db.db_instance.address,
    },
)
