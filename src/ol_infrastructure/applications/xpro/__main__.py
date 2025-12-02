"""Create the infrastructure and services needed to support the MIT XPro application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
from pathlib import Path

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import Config, InvokeOptions, StackReference, export
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider(skip_child_token=True)
setup_heroku_provider()

xpro_config = Config("xpro")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

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
xpro_storage_bucket = s3.Bucket(
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
s3.BucketVersioning(
    "ol-xpro-app-bucket-versioning",
    bucket=xpro_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
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
s3.BucketPolicy(
    "ol-xpro-app-bucket-policy",
    bucket=xpro_storage_bucket.id,
    policy=iam.get_policy_document(
        statements=[
            iam.GetPolicyDocumentStatementArgs(
                effect="Allow",
                principals=[
                    iam.GetPolicyDocumentStatementPrincipalArgs(
                        type="AWS", identifiers=["*"]
                    )
                ],
                actions=["s3:GetObject"],
                resources=[xpro_storage_bucket.arn.apply("{}/*".format)],
            )
        ]
    ).json,
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
    iam_tags={"OU": "operations", "vault_managed": "True"},
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
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
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
    engine_major_version="17",
    tags=aws_config.tags,
    db_name="xpro",
    public_access=True,
    parameter_overrides=[{"name": "password_encryption", "value": "md5"}],
    **defaults(stack_info)["rds"],
)
xpro_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
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
# Don't create the mount here because it is already created as part of
# ol_infrastructure/applications/edxapp. Instead we want to factor the creation of all
# mount points to a single substructure project and use stack references for all usages
# to populate or retrieve values in the resepective mounts.
# TMM 2023-11-14

xpro_vault_secrets = read_yaml_secrets(
    Path(f"xpro/secrets.{stack_info.env_suffix}.yaml")
)

for key, data in xpro_vault_secrets.items():
    vault.kv.Secret(
        f"xpro-vault-secrets-{key}",
        # This mount is created already as part of the edxapp Pulumi project. See note
        # above for future work.
        path=f"secret-xpro/{key}",
        data_json=json.dumps(data),
    )

# Heroku App configuration
# env_name is 'ci' 'rc' or 'production'
env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# Values that are generally unchanging across environments
heroku_vars = {
    "AWS_STORAGE_BUCKET_NAME": xpro_storage_bucket_name,
    "CERTIFICATE_CREATION_DELAY_IN_HOURS": 48,
    "CRON_COURSERUN_SYNC_HOURS": "*",
    "CYBERSOURCE_MERCHANT_ID": "mit_odl_xpro",
    "CYBERSOURCE_REFERENCE_PREFIX": f"xpro-{env_name}",
    "HUBSPOT_PIPELINE_ID": "75e28846-ad0d-4be2-a027-5e1da6590b98",
    "MITOL_DIGITAL_CREDENTIALS_AUTH_TYPE": "code",
    "MITOL_DIGITAL_CREDENTIALS_DEEP_LINK_URL": "dccrequest://request",
    "MITXPRO_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITXPRO_DB_CONN_MAX_AGE": 0,
    "MITXPRO_DB_DISABLE_SSL": "True",
    "MITXPRO_EMAIL_TLS": "True",
    "MITXPRO_ENVIRONMENT": env_name,
    "MITXPRO_FROM_EMAIL": "MIT xPRO <support@xpro.mit.edu>",
    "MITXPRO_OAUTH_PROVIDER": "mitxpro-oauth2",
    "MITXPRO_REPLY_TO_ADDRESS": "MIT xPRO <support@xpro.mit.edu>",
    "MITXPRO_SECURE_SSL_REDIRECT": "True",
    "MITXPRO_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OAUTH2_PROVIDER_ALLOWED_REDIRECT_URI_SCHEMES": "http,https,dccrequest",
    "POSTHOG_ENABLED": "True",
    "POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    # This can be removed once PR#1314 is in production,
    "OPENEDX_OAUTH_APP_NAME": "edx-oauth-app",
    # This replaces OPENEDX_GRADES_API_TOKEN and is
    # tied to xpro-grades-api user in openedx,
    "OPENEDX_SERVICE_WORKER_USERNAME": "xpro-service-worker-api",
    "PGBOUNCER_DEFAULT_POOL_SIZE": 50,
    "PGBOUNCER_MIN_POOL_SIZE": 5,
    "SHEETS_DATE_TIMEZONE": "America/New_York",
    "SHEETS_TASK_OFFSET": "120",
    "SHOW_UNREDEEMED_COUPON_ON_DASHBOARD": "True",
    "SITE_NAME": "MIT xPRO",
    "USE_X_FORWARDED_HOST": "True",
}

# Combine var source above with values explictly defined in pulumi configuration file
heroku_vars.update(**heroku_app_config.get_object("vars"))

# Finally, populate a map of vars that contain secrets
auth_aws_mitx_creds_xpro_app = vault.generic.get_secret_output(
    path="aws-mitx/creds/xpro-app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)
auth_postgres_xpro_creds_app = vault.generic.get_secret_output(
    path="postgres-xpro/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)
secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)

sensitive_heroku_vars = {
    # Secrets that can be source locally from SOPS
    "COUPON_REQUEST_SHEET_ID": xpro_vault_secrets["google-sheets"]["sheet_id"],
    "CYBERSOURCE_ACCESS_KEY": xpro_vault_secrets["cybersource"]["access_key"],
    "CYBERSOURCE_INQUIRY_LOG_NACL_ENCRYPTION_KEY": xpro_vault_secrets["cybersource"][
        "inquiry_log_nacl_encryption_key"
    ],
    "CYBERSOURCE_PROFILE_ID": xpro_vault_secrets["cybersource"]["profile_id"],
    "CYBERSOURCE_SECURITY_KEY": xpro_vault_secrets["cybersource"]["security_key"],
    "CYBERSOURCE_TRANSACTION_KEY": xpro_vault_secrets["cybersource"]["transaction_key"],
    "DEFERRAL_REQUEST_WORKSHEET_ID": xpro_vault_secrets["google-sheets"][
        "deferral_worksheet_id"
    ],
    "DIGITAL_CREDENTIALS_ISSUER_ID": xpro_vault_secrets["digital-credentials"][
        "issuer_id"
    ],
    "DIGITAL_CREDENTIALS_OAUTH2_CLIENT_ID": xpro_vault_secrets["digital-credentials"][
        "oauth2_client_id"
    ],
    "DIGITAL_CREDENTIALS_VERIFICATION_METHOD": xpro_vault_secrets[
        "digital-credentials"
    ]["verification_method"],
    "DRIVE_OUTPUT_FOLDER_ID": xpro_vault_secrets["google-sheets"]["folder_id"],
    "DRIVE_SERVICE_ACCOUNT_CREDS": xpro_vault_secrets["google-sheets"][
        "service_account_creds"
    ],
    "DRIVE_SHARED_ID": xpro_vault_secrets["google-sheets"]["drive_shared_id"],
    "EMERITUS_API_KEY": xpro_vault_secrets["emeritus"]["api_key"],
    "ENROLLMENT_CHANGE_SHEET_ID": xpro_vault_secrets["google-sheets"][
        "enroll_change_sheet_id"
    ],
    "EXTERNAL_COURSE_SYNC_API_KEY": xpro_vault_secrets["external-course-sync"][
        "api_key"
    ],
    "EXTERNAL_COURSE_SYNC_EMAIL_RECIPIENTS": xpro_vault_secrets["external-course-sync"][
        "email-recipients"
    ],
    "HIREFIRE_TOKEN": xpro_vault_secrets["hirefire"]["token"],
    "MITOL_DIGITAL_CREDENTIALS_HMAC_SECRET": xpro_vault_secrets["digital-credentials"][
        "hmac_secret"
    ],
    "MITOL_DIGITAL_CREDENTIALS_VERIFY_SERVICE_BASE_URL": xpro_vault_secrets[
        "digital-credentials"
    ]["sign_and_verify_url"],
    "MITOL_HUBSPOT_API_PRIVATE_TOKEN": xpro_vault_secrets["hubspot"][
        "api_private_token"
    ],
    "MITXPRO_EMAIL_HOST": xpro_vault_secrets["smtp"]["relay_host"],
    "MITXPRO_EMAIL_PASSWORD": xpro_vault_secrets["smtp"]["relay_password"],
    "MITXPRO_EMAIL_PORT": xpro_vault_secrets["smtp"]["relay_port"],
    "MITXPRO_EMAIL_USER": xpro_vault_secrets["smtp"]["relay_username"],
    "MITXPRO_REGISTRATION_ACCESS_TOKEN": xpro_vault_secrets["openedx"][
        "registration_access_token"
    ],
    "MITXPRO_SUPPORT_EMAIL": xpro_vault_secrets["smtp"]["support_email"],
    "OPENEDX_API_CLIENT_ID": xpro_vault_secrets["openedx-api-client"]["client_id"],
    "OPENEDX_API_CLIENT_SECRET": xpro_vault_secrets["openedx-api-client"][
        "client_secret"
    ],
    "OPENEDX_GRADES_API_TOKEN": xpro_vault_secrets["openedx"]["grades_api_token"],
    "OPENEDX_SERVICE_WORKER_API_TOKEN": xpro_vault_secrets["openedx"][
        "service_worker_api_token"
    ],
    "POSTHOG_PROJECT_API_KEY": xpro_vault_secrets.get("posthog", {}).get(
        "project_api_key", ""
    ),
    "RECAPTCHA_SECRET_KEY": xpro_vault_secrets["recaptcha"]["secret_key"],
    "RECAPTCHA_SITE_KEY": xpro_vault_secrets["recaptcha"]["site_key"],
    "REFUND_REQUEST_WORKSHEET_ID": xpro_vault_secrets["google-sheets"][
        "refund_worksheet_id"
    ],
    "SECRET_KEY": xpro_vault_secrets["django"]["secret-key"],
    "SENTRY_DSN": xpro_vault_secrets["sentry"]["dsn"],
    "SHEETS_ADMIN_EMAILS": xpro_vault_secrets["google-sheets"]["admin_emails"],
    "STATUS_TOKEN": xpro_vault_secrets["django"]["status-token"],
    "VOUCHER_DOMESTIC_AMOUNT_KEY": xpro_vault_secrets["voucher-domestic"]["amount_key"],
    "VOUCHER_DOMESTIC_COURSE_KEY": xpro_vault_secrets["voucher-domestic"]["course_key"],
    "VOUCHER_DOMESTIC_CREDITS_KEY": xpro_vault_secrets["voucher-domestic"][
        "credits_key"
    ],
    "VOUCHER_DOMESTIC_DATES_KEY": xpro_vault_secrets["voucher-domestic"]["dates_key"],
    "VOUCHER_DOMESTIC_DATE_KEY": xpro_vault_secrets["voucher-domestic"]["date_key"],
    "VOUCHER_DOMESTIC_EMPLOYEE_ID_KEY": xpro_vault_secrets["voucher-domestic"][
        "employee_id_key"
    ],
    "VOUCHER_DOMESTIC_EMPLOYEE_KEY": xpro_vault_secrets["voucher-domestic"][
        "employee_key"
    ],
    "VOUCHER_DOMESTIC_KEY": xpro_vault_secrets["voucher-domestic"]["key"],
    # Auth secrets that require something more involved
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_xpro_app.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_xpro_app.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "DATABASE_URL": auth_postgres_xpro_creds_app.data.apply(
        lambda data: "postgres://{}:{}@xpro-db-applications-{}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/xpro".format(
            data["username"], data["password"], stack_info.name.lower()
        )
    ),
    # Static secrets that require something more involved
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
}


# Put it all together into a ConfigAssociation resource
heroku_app_id = heroku_config.require("app_id")
xpro_heroku_configassociation = heroku.app.ConfigAssociation(
    f"xpro-{stack_info.env_suffix}-heroku-configassociation",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export(
    "xpro_app",
    {
        "rds_host": xpro_db.db_instance.address,
    },
)
