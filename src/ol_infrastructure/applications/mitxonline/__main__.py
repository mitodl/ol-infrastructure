# ruff: noqa: E501

"""Create the infrastructure and services needed to support the MITx Online application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from pulumi import Config, InvokeOptions, StackReference, export
from pulumi_aws import ec2, iam, s3

from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider(skip_child_token=True)
setup_heroku_provider()

mitxonline_config = Config("mitxonline")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

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
    f"yymitxonline-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the MITx Online application in the "
        f"{stack_info.name} environment"
    ),
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

db_defaults = {**defaults(stack_info)["rds"]}
if stack_info.name == "QA":
    db_defaults["instance_size"] = DBInstanceTypes.general_purpose_large
mitxonline_db_config = OLPostgresDBConfig(
    instance_name=f"mitxonline-{stack_info.env_suffix}-app-db",
    password=mitxonline_config.require("db_password"),
    subnet_group_name=mitxonline_vpc["rds_subnet"],
    security_groups=[mitxonline_db_security_group],
    engine_major_version="13",
    tags=aws_config.tags,
    db_name="mitxonline",
    public_access=True,
    **db_defaults,
)
mitxonline_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
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

heroku_vars = {
    "CRON_COURSERUN_SYNC_HOURS": "*",
    "MITX_ONLINE_SUPPORT_EMAIL": "mitxonline-support@mit.edu",
    "FEATURE_SYNC_ON_DASHBOARD_LOAD": "True",
    "FEATURE_IGNORE_EDX_FAILURES": "True",
    "HUBSPOT_PIPELINE_ID": "19817792",
    "MITOL_GOOGLE_SHEETS_REFUNDS_COMPLETED_DATE_COL": "12",
    "MITOL_GOOGLE_SHEETS_REFUNDS_ERROR_COL": "13",
    "MITOL_GOOGLE_SHEETS_REFUNDS_SKIP_ROW_COL": "14",
    "MITX_ONLINE_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITX_ONLINE_DB_CONN_MAX_AGE": "0",
    "MITX_ONLINE_DB_DISABLE_SSL": "True",  # pgbouncer buildpack uses stunnel to handle encryption"
    "MITX_ONLINE_FROM_EMAIL": "MITx Online <support@mitxonline.mit.edu>",
    "MITX_ONLINE_OAUTH_PROVIDER": "mitxonline-oauth2",
    "MITX_ONLINE_REPLY_TO_ADDRESS": "MITx Online <support@mitxonline.mit.edu>",
    "MITX_ONLINE_SECURE_SSL_REDIRECT": "True",
    "MITX_ONLINE_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OPEN_EXCHANGE_RATES_URL": "https://openexchangerates.org/api/",
    "OPENEDX_SERVICE_WORKER_USERNAME": "login_service_user",
    "PGBOUNCER_DEFAULT_POOL_SIZE": "50",
    "PGBOUNCER_MIN_POOL_SIZE": "5",
    "SITE_NAME": "MITx Online",
    "USE_X_FORWARDED_HOST": "True",
    "ZENDESK_HELP_WIDGET_ENABLED": "True",
    "POSTHOG_API_HOST": "https://app.posthog.com",
    "POSTHOG_ENABLED": "True",
}

# All of the secrets for this app must be obtained with async incantations

env_name = (
    stack_info.env_suffix.lower() if stack_info.env_suffix.lower() != "qa" else "rc"
)
openedx_environment = f"mitxonline-{stack_info.env_suffix.lower()}"

rds_endpoint = f"mitxonline-{stack_info.env_suffix.lower()}-app-db.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432"

auth_aws_mitx_creds_mitxonline = vault.generic.get_secret_output(
    path="aws-mitx/creds/mitxonline",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

auth_postgres_mitxonline_creds_app = vault.generic.get_secret_output(
    path="postgres-mitxonline/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_google_sheets_refunds = vault.generic.get_secret_output(
    path="secret-mitxonline/google-sheets-refunds",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_refine_oidc = vault.generic.get_secret_output(
    path="secret-mitxonline/refine-oidc",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_open_exchange_rates = vault.generic.get_secret_output(
    path="secret-mitxonline/open-exchange-rates",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_recaptcha_keys = vault.generic.get_secret_output(
    path="secret-mitxonline/recaptcha-keys",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_openedx_retirement_service_worker = vault.generic.get_secret_output(
    path="secret-mitxonline/openedx-retirement-service-worker",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_env_cybersource_credentials = vault.generic.get_secret_output(
    path=f"secret-mitxonline/{env_name}/cybersource-credentials",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_hubspot_api_private_token = vault.generic.get_secret_output(
    path="secret-mitxonline/hubspot-api-private-token",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_openedx_env_mitxonline_registration_access_token = vault.generic.get_secret_output(
    path=f"secret-mitxonline/{openedx_environment}/mitxonline-registration-access-token",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_env_openedx_api_client = vault.generic.get_secret_output(
    path=f"secret-mitxonline/{env_name}/openedx-api-client",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_env_openedx_service_worker_api_token = (
    vault.generic.get_secret_output(
        path=f"secret-mitxonline/{env_name}/openedx-service-worker-api-token",
        opts=InvokeOptions(parent=mitxonline_vault_backend_role),
    )
)

secret_mitxonline_env_django_secret_key = vault.generic.get_secret_output(
    path=f"secret-mitxonline/{env_name}/django-secret-key",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_env_django_status_token = vault.generic.get_secret_output(
    path=f"secret-mitxonline/{env_name}/django-status-token",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_posthog_credentials = vault.generic.get_secret_output(
    path="secret-mitxonline/posthog-credentials",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

secret_mitxonline_hubspot = vault.generic.get_secret_output(
    path="secret-mitxonline/hubspot",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)
secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)
secret_operations_global_mitxonline_sentry_dsn = vault.generic.get_secret_output(
    path="secret-operations/global/mitxonline/sentry-dsn",
    opts=InvokeOptions(parent=mitxonline_vault_backend_role),
)

sensitive_heroku_vars = {
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_mitxonline.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_mitxonline.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "DATABASE_URL": auth_postgres_mitxonline_creds_app.data.apply(
        lambda data: "postgres://{}:{}@{}/mitxonline".format(
            data["username"], data["password"], rds_endpoint
        )
    ),
    "HUBSPOT_HOME_PAGE_FORM_GUID": secret_mitxonline_hubspot.data.apply(
        lambda data: "{}".format(data["formId"])
    ),
    "HUBSPOT_PORTAL_ID": secret_mitxonline_hubspot.data.apply(
        lambda data: "{}".format(data["portalId"])
    ),
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "MITOL_GOOGLE_SHEETS_DRIVE_API_PROJECT_ID": secret_mitxonline_google_sheets_refunds.data.apply(
        lambda data: "{}".format(data["drive-api-project-id"])
    ),
    "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_ID": secret_mitxonline_google_sheets_refunds.data.apply(
        lambda data: "{}".format(data["drive-client-id"])
    ),
    "MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_SECRET": secret_mitxonline_google_sheets_refunds.data.apply(
        lambda data: "{}".format(data["drive-client-secret"])
    ),
    "MITOL_GOOGLE_SHEETS_ENROLLMENT_CHANGE_SHEET_ID": secret_mitxonline_google_sheets_refunds.data.apply(
        lambda data: "{}".format(data["enrollment-change-sheet-id"])
    ),
    "MITOL_HUBSPOT_API_PRIVATE_TOKEN": secret_mitxonline_hubspot_api_private_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_ACCESS_KEY": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["access-key"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_ID": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["merchant-id"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["merchant-secret"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_MERCHANT_SECRET_KEY_ID": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["merchant-secret-key-id"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_PROFILE_ID": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["profile-id"])
    ),
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_SECURITY_KEY": secret_mitxonline_env_cybersource_credentials.data.apply(
        lambda data: "{}".format(data["security-key"])
    ),
    "MITX_ONLINE_REFINE_OIDC_CONFIG_CLIENT_ID": secret_mitxonline_refine_oidc.data.apply(
        lambda data: "{}".format(data["client-id"])
    ),
    "MITX_ONLINE_REGISTRATION_ACCESS_TOKEN": secret_mitxonline_openedx_env_mitxonline_registration_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OIDC_RSA_PRIVATE_KEY": secret_mitxonline_refine_oidc.data.apply(
        lambda data: "{}".format(data["rsa-private-key"])
    ),
    "OPENEDX_API_CLIENT_ID": secret_mitxonline_env_openedx_api_client.data.apply(
        lambda data: "{}".format(data["client-id"])
    ),
    "OPENEDX_API_CLIENT_SECRET": secret_mitxonline_env_openedx_api_client.data.apply(
        lambda data: "{}".format(data["client-secret"])
    ),
    "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_ID": secret_mitxonline_openedx_retirement_service_worker.data.apply(
        lambda data: "{}".format(data["client_id"])
    ),
    "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_SECRET": secret_mitxonline_openedx_retirement_service_worker.data.apply(
        lambda data: "{}".format(data["client_secret"])
    ),
    "OPENEDX_SERVICE_WORKER_API_TOKEN": secret_mitxonline_env_openedx_service_worker_api_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OPEN_EXCHANGE_RATES_APP_ID": secret_mitxonline_open_exchange_rates.data.apply(
        lambda data: "{}".format(data["app_id"])
    ),
    "POSTHOG_PROJECT_API_KEY": secret_mitxonline_posthog_credentials.data.apply(
        lambda data: "{}".format(data["api-token"])
    ),
    "POSTHOG_API_TOKEN": secret_mitxonline_posthog_credentials.data.apply(
        lambda data: "{}".format(data["api-token"])
    ),
    "RECAPTCHA_SECRET_KEY": secret_mitxonline_recaptcha_keys.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "RECAPTCHA_SITE_KEY": secret_mitxonline_recaptcha_keys.data.apply(
        lambda data: "{}".format(data["site_key"])
    ),
    "SECRET_KEY": secret_mitxonline_env_django_secret_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "SENTRY_DSN": secret_operations_global_mitxonline_sentry_dsn.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "STATUS_TOKEN": secret_mitxonline_env_django_status_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
}

heroku_vars.update(**heroku_app_config.get_object("vars"))

heroku_app_id = heroku_config.require("app_id")
mitxonline_heroku_configassociation = heroku.app.ConfigAssociation(
    f"mitxonline-{stack_info.env_suffix}-heroku-configassociation",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export("mitxonline_app", {"rds_host": mitxonline_db.db_instance.address})
