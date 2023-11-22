import json
from pathlib import Path

import pulumi_vault as vault
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT, FIVE_MINUTES
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi.output import Output
from pulumi_aws import ec2, iam, s3

from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
mitopen_config = Config("mitopen")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
aws_config = AWSBase(
    tags={
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": "mitopen",
    }
)
app_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]

app_storage_bucket_name = f"ol-mitopen-app-storage-{app_env_suffix}"
application_storage_bucket = s3.Bucket(
    f"ol_mitopen_app_storage_bucket_{stack_info.env_suffix}",
    bucket=app_storage_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

course_data_bucket_name = f"ol-mitopen-course-data-{app_env_suffix}"
course_data_bucket = s3.Bucket(
    f"ol_mitopen_course_data_bucket_{stack_info.env_suffix}",
    bucket=course_data_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    cors_rules=[
        s3.BucketCorsRuleArgs(
            allowed_methods=["GET"],
            allowed_headers=["*"],
            allowed_origins=["*"],
            max_age_seconds=FIVE_MINUTES,
        )
    ],
    tags=aws_config.tags,
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "RESOURCE_EFFECTIVELY_STAR": {},
}

# TODO: MD 07312023 Requires review of bucket names  # noqa: FIX002, TD002, TD003
s3_bucket_permissions = [
    {
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "S3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}",
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}/*",
            f"arn:aws:s3:::{app_storage_bucket_name}",
            f"arn:aws:s3:::{app_storage_bucket_name}/*",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}/*",
        ],
    },
    {
        "Action": ["s3:GetObject*", "s3:ListBucket*"],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses",
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses/*",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production/*",
            "arn:aws:s3:::mitx-etl-mitxonline-production",
            "arn:aws:s3:::mitx-etl-mitxonline-production/*",
            "arn:aws:s3:::ol-olx-course-exports",
            "arn:aws:s3:::ol-olx-course-exports/*",
            "arn:aws:s3:::ocw-content-storage",
            "arn:aws:s3:::ocw-content-storage/*",
            f"arn:aws:s3:::ol-ocw-studio-app-{app_env_suffix}",
        ],
    },
]

open_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": s3_bucket_permissions,
}

mitopen_iam_policy = iam.Policy(
    f"ol_mitopen_iam_permissions_{stack_info.env_suffix}",
    name=f"ol-mitopen-application-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mitopen/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        open_policy_document, stringify=True, parliament_config=parliament_config
    ),
)

mitopen_vault_iam_role = vault.aws.SecretBackendRole(
    f"ol-mitopen-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name="ol-mitopen-application",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mitopen_iam_policy.arn],
)

mitopen_vault_mount = vault.Mount(
    f"ol-mitopen-configuration-secrets-mount-{stack_info.env_suffix}",
    path="secret-mitopen",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration secrets used by MIT-Open",
    opts=ResourceOptions(delete_before_replace=True),
)

mitopen_vault_secrets = read_yaml_secrets(
    Path(f"mitopen/secrets.{stack_info.env_suffix}.yaml"),
)

vault.generic.Secret(
    f"ol-mitopen-configuration-secrets-{stack_info.env_suffix}",
    path=mitopen_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(mitopen_vault_secrets),
)

mitopen_db_security_group = ec2.SecurityGroup(
    f"ol-mitopen-db-access-{stack_info.env_suffix}",
    description=f"Access control for the MIT Open application DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku.",
        )
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/32"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.tags,
    vpc_id=apps_vpc["id"],
)

rds_password = mitopen_config.require("db_password")
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    mitopen_config.get("db_instance_size") or rds_defaults["instance_size"]
)
mitopen_db_config = OLPostgresDBConfig(
    instance_name=f"ol-mitopen-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[mitopen_db_security_group],
    tags=aws_config.tags,
    db_name="mitopen",
    public_access=True,
    **rds_defaults,
)
mitopen_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)

mitopen_db = OLAmazonDB(mitopen_db_config)

mitopen_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=mitopen_db_config.db_name,
    mount_point=f"{mitopen_db_config.engine}-mitopen",
    db_admin_username=mitopen_db_config.username,
    db_admin_password=rds_password,
    db_host=mitopen_db.db_instance.address,
)
mitopen_vault_backend = OLVaultDatabaseBackend(mitopen_vault_backend_config)

export(
    "mitopen",
    {
        "iam_policy": mitopen_iam_policy.arn,
        "vault_iam_role": Output.all(
            mitopen_vault_iam_role.backend, mitopen_vault_iam_role.name
        ).apply(lambda role: f"{role[0]}/roles/{role[1]}"),
    },
)
