"""Create the infrastructure and services needed to support the MITx Online application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json

import pulumi_consul as consul
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

mitxonline_config = Config("mitxonline")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
mitxonline_vpc = network_stack.require_output("mitxonline_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
mitxonline_environment = f"mitxonline-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": "mitxonline",
        "Environment": mitxonline_environment,
        "Application": "mitxonline",
    }
)

# Create S3 bucket

# Bucket used to store files from MITx Online app.
mitxonline_bucket_name = f"ol-mitxonline-app-{stack_info.env_suffix}"
mitxonline_bucket = s3.Bucket(
    f"mitxonline-{stack_info.env_suffix}",
    bucket=mitxonline_bucket_name,
    versioning=s3.BucketVersioningArgs(
        enabled=True,
    ),
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{mitxonline_bucket_name}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)


mitxonline_iam_policy = iam.Policy(
    f"mitxonline-{stack_info.env_suffix}-policy",
    description="AWS access controls for the MITx Online application in the "
    f"{stack_info.name} environment",
    path=f"/ol-applications/mitxonline/{stack_info.env_suffix}/",
    name_prefix=f"mitxonline-{stack_info.env_suffix}-application-policy-",
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
                        f"arn:aws:s3:::{mitxonline_bucket_name}",
                        f"arn:aws:s3:::{mitxonline_bucket_name}/*",
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

mitxonline_vault_backend_role = vault.aws.SecretBackendRole(
    "mitxonline-app",
    name="mitxonline",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mitxonline_iam_policy.arn],
)

# Create RDS instance
mitxonline_db_security_group = ec2.SecurityGroup(
    f"mitxonline-db-access-{stack_info.env_suffix}",
    description=f"Access control for the MITx Online App DB in {stack_info.name}",
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
        {"Name": "mitxonline-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=mitxonline_vpc["id"],
)

mitxonline_db_config = OLPostgresDBConfig(
    instance_name=f"mitxonline-{stack_info.env_suffix}-app-db",
    password=mitxonline_config.require("db_password"),
    subnet_group_name=mitxonline_vpc["rds_subnet"],
    security_groups=[mitxonline_db_security_group],
    tags=aws_config.tags,
    db_name="mitxonline",
    public_access=True,
    **defaults(stack_info)["rds"],
)
mitxonline_db = OLAmazonDB(mitxonline_db_config)

mitxonline_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=mitxonline_db_config.db_name,
    mount_point=f"{mitxonline_db_config.engine}-mitxonline",
    db_admin_username=mitxonline_db_config.username,
    db_admin_password=mitxonline_db_config.password.get_secret_value(),
    db_host=mitxonline_db.db_instance.address,
)
mitxonline_vault_backend = OLVaultDatabaseBackend(mitxonline_vault_backend_config)

# Set Consul key for use in edxapp configuration template
consul.Keys(
    "mitxonline-app-domain-for-edxapp",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/marketing-domain",
            value=mitxonline_config.require("domain"),
        ),
        consul.KeysKeyArgs(
            path="edxapp/proctortrack-base-url",
            value=mitxonline_config.require("proctortrack_url"),
        ),
    ],
)

export("mitxonline_app", {"rds_host": mitxonline_db.db_instance.address})
