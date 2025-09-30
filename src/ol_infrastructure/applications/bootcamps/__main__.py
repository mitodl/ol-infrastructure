"""Create the infrastructure and services needed to support the bootcamps application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
from pathlib import Path

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference, export
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider(skip_child_token=True)
setup_heroku_provider()

bootcamps_config = Config("bootcamps")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")
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
    tags=aws_config.tags,
)
bootcamps_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-bootcamps-app-bucket-ownership-controls",
    bucket=bootcamps_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-bootcamps-app-bucket-versioning",
    bucket=bootcamps_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
bootcamps_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-bootcamps-app-bucket-public-access",
    bucket=bootcamps_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-bootcamps-app-bucket-policy",
    bucket=bootcamps_storage_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"arn:aws:s3:::{bootcamps_storage_bucket_name}/images/*",
                        f"arn:aws:s3:::{bootcamps_storage_bucket_name}/resumes/*",
                        f"arn:aws:s3:::{bootcamps_storage_bucket_name}/documents/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            bootcamps_storage_bucket_public_access,
            bootcamps_storage_bucket_ownership_controls,
        ]
    ),
)

bootcamps_iam_policy = iam.Policy(
    f"bootcamps-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the bootcamps application in the "
        f"{stack_info.name} environment"
    ),
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
            "RESOURCE_MISMATCH": {},
        },
    ),
)

bootcamps_vault_backend_role = vault.aws.SecretBackendRole(
    "bootcamps-app",
    name="bootcamps-app",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
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
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
        ),
    ],
    tags=aws_config.merged_tags(
        {"name": f"bootcamps-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
bootcamps_db_config = OLPostgresDBConfig(
    instance_name=f"bootcamps-db-applications-{stack_info.env_suffix}",
    password=bootcamps_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[bootcamps_db_security_group],
    engine_major_version="13",
    tags=aws_config.tags,
    db_name="bootcamps",
    public_access=True,
    **rds_defaults,
)
bootcamps_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)
bootcamps_db = OLAmazonDB(bootcamps_db_config)

bootcamps_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=bootcamps_db_config.db_name,
    mount_point=f"{bootcamps_db_config.engine}-bootcamps",
    db_admin_username=bootcamps_db_config.username,
    db_admin_password=bootcamps_db_config.password.get_secret_value(),
    db_host=bootcamps_db.db_instance.address,
    **rds_defaults,
)
bootcamps_vault_backend = OLVaultDatabaseBackend(bootcamps_vault_backend_config)

######################
# Secrets Management #
######################
bootcamps_secrets = vault.Mount(
    "bootcamps-vault-secrets-storage",
    path="secret-bootcamps",
    type="kv",
    options={"version": 2},
    description="Static secrets storage for the bootcamps application",
    opts=ResourceOptions(delete_before_replace=True),
)

bootcamps_vault_secrets = read_yaml_secrets(
    Path(f"bootcamps/secrets.{stack_info.env_suffix}.yaml")
)

for key, data in bootcamps_vault_secrets.items():
    vault.kv.SecretV2(
        f"bootcamps-vault-secrets-{key}",
        name=key,
        mount=bootcamps_secrets,
        data_json=json.dumps(data),
    )

heroku_vars = {
    "BOOTCAMP_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "BOOTCAMP_DB_DISABLE_SSL": "True",
    "BOOTCAMP_EMAIL_TLS": "True",
    "BOOTCAMP_REPLY_TO_ADDRESS": "MIT Bootcamps <bootcamps-support@mit.edu>",
    "BOOTCAMP_SECURE_SSL_REDIRECT": "True",
    "BOOTCAMP_USE_S3": "True",
    "ENABLE_STUNNEL_AMAZON_RDS_FIX": "True",
    "FEATURE_ENABLE_CERTIFICATE_USER_VIEW": "True",
    "FEATURE_SOCIAL_AUTH_API": "True",
    "FEATURE_CMS_HOME_PAGE": "True",
    "JOBMA_LINK_EXPIRATION_DAYS": 13,
    "MAX_FILE_UPLOAD_MB": 10,
    "NODE_MODULES_CACHE": "False",
    "NOVOED_API_BASE_URL": "https://api.novoed.com/",
    "PGBOUNCER_DEFAULT_POOL_SIZE": 50,
    "PGBOUNCER_MIN_POOL_SIZE": 5,
    "ZENDESK_HELP_WIDGET_ENABLED": "True",
}

sensitive_heroku_vars = {
    "CYBERSOURCE_ACCESS_KEY": "",
    "CYBERSOURCE_PROFILE_ID": bootcamps_vault_secrets["cybersource"]["profile_id"],
    "CYBERSOURCE_SECURITY_KEY": bootcamps_vault_secrets["cybersource"]["security_key"],
    "EDXORG_CLIENT_ID": bootcamps_vault_secrets["edx"]["client_id"],
    "EDXORG_CLIENT_SECRET": bootcamps_vault_secrets["edx"]["client_secret"],
    "JOBMA_ACCESS_TOKEN": bootcamps_vault_secrets["jobma"]["access_token"],
    "JOBMA_WEBHOOK_ACCESS_TOKEN": bootcamps_vault_secrets["jobma"][
        "webhook_access_token"
    ],
    "NOVOED_API_KEY": bootcamps_vault_secrets["novoed"]["api_key"],
    "NOVOED_API_SECRET": bootcamps_vault_secrets["novoed"]["api_secret"],
    "NOVOED_SAML_CERT": bootcamps_vault_secrets["novoed"]["saml_cert"],
    "NOVOED_SAML_KEY": bootcamps_vault_secrets["novoed"]["saml_key"],
    "RECAPTCHA_SECRET_KEY": bootcamps_vault_secrets["recaptcha"]["secret_key"],
    "RECAPTCHA_SITE_KEY": bootcamps_vault_secrets["recaptcha"]["site_key"],
    "SECRET_KEY": bootcamps_vault_secrets["django"]["secret_key"],
    "SENTRY_DSN": bootcamps_vault_secrets["sentry"]["dsn"],
    "STATUS_TOKEN": bootcamps_vault_secrets["django"]["status_token"],
}

auth_aws_mitx_creds_bootcamps_app = vault.generic.get_secret_output(
    path="aws-mitx/creds/bootcamps-app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=bootcamps_secrets),
)

sensitive_heroku_vars["AWS_ACCESS_KEY_ID"] = (
    auth_aws_mitx_creds_bootcamps_app.data.apply(
        lambda data: "{}".format(data["access_key"])
    )
)
sensitive_heroku_vars["AWS_SECRET_ACCESS_KEY"] = (
    auth_aws_mitx_creds_bootcamps_app.data.apply(
        lambda data: "{}".format(data["secret_key"])
    )
)

auth_postgres_bootcamps_creds_app = vault.generic.get_secret_output(
    path=f"{bootcamps_vault_backend_config.mount_point}/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=bootcamps_secrets),
)

sensitive_heroku_vars["DATABASE_URL"] = auth_postgres_bootcamps_creds_app.data.apply(
    lambda data: "postgres://{}:{}@bootcamps-db-applications-{}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/bootcamps".format(
        data["username"], data["password"], stack_info.env_suffix
    )
)

secret_global_mailgun = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=bootcamps_secrets),
)
sensitive_heroku_vars["MAILGUN_KEY"] = secret_global_mailgun.data.apply(
    lambda data: "{}".format(data["api_key"])
)

secret_global_mit_smtp = vault.generic.get_secret_output(
    path="secret-global/mit-smtp",
    opts=InvokeOptions(parent=bootcamps_secrets),
)

sensitive_heroku_vars["BOOTCAMP_EMAIL_HOST"] = secret_global_mit_smtp.data.apply(
    lambda data: "{}".format(data["relay_host"])
)
sensitive_heroku_vars["BOOTCAMP_EMAIL_PASSWORD"] = secret_global_mit_smtp.data.apply(
    lambda data: "{}".format(data["relay_password"])
)
sensitive_heroku_vars["BOOTCAMP_EMAIL_USER"] = secret_global_mit_smtp.data.apply(
    lambda data: "{}".format(data["relay_username"])
)
sensitive_heroku_vars["BOOTCAMP_EMAIL_PORT"] = secret_global_mit_smtp.data.apply(
    lambda data: "{}".format(data["relay_port"])
)

# Only production has a hirefire token configured
if stack_info.env_suffix == "production":
    sensitive_heroku_vars["HIREFIRE_TOKEN"] = bootcamps_vault_secrets["hirefire"][
        "token"
    ]

heroku_vars.update(**heroku_app_config.get_object("vars"))

heroku_app_id = heroku_config.require("app_id")
bootcamps_heroku_configassociation = heroku.app.ConfigAssociation(
    f"bootcamps-heroku-configassociation-{stack_info.env_suffix}",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export(
    "bootcamps_app",
    {
        "rds_host": bootcamps_db.db_instance.address,
    },
)
