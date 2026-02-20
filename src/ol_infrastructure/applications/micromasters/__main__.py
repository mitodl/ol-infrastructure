"""Create the infrastructure and services needed to support the
MicroMasters application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
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

setup_vault_provider(skip_child_token=True)
setup_heroku_provider()

micromasters_config = Config("micromasters")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

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

micromasters_bucket_config = S3BucketConfig(
    bucket_name=micromasters_bucket_name,
    versioning_enabled=True,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    ownership_controls="BucketOwnerEnforced",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    tags=aws_config.tags,
)

micromasters_bucket = OLBucket(
    f"micromasters-{stack_info.env_suffix}",
    config=micromasters_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"micromasters-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
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
    iam_tags={"OU": "operations", "vault_managed": "True"},
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
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
micromasters_db_config = OLPostgresDBConfig(
    instance_name=f"micromasters-{stack_info.env_suffix}-app-db",
    password=micromasters_config.require("db_password"),
    subnet_group_name=micromasters_vpc["rds_subnet"],
    security_groups=[micromasters_db_security_group],
    tags=aws_config.tags,
    db_name="micromasters",
    engine_major_version="15",
    public_access=True,
    **rds_defaults,
)
micromasters_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
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

heroku_vars = {
    "BATCH_UPDATE_RATE_LIMIT": "2/m",
    "CLIENT_ELASTICSEARCH_URL": "/api/v0/search/",
    "CRONTAB_DISCUSSIONS_SYNC": "0 9 * * *",
    "ENABLE_STUNNEL_AMAZON_RDS_FIX": "True",
    "FEATURE_ENABLE_PROGRAM_LETTER": "True",
    "FEATURE_FINAL_GRADE_ALGORITHM": "v1",
    "FEATURE_MITXONLINE_LOGIN": "True",
    "FEATURE_OPEN_DISCUSSIONS_CREATE_CHANNEL_UI": "True",
    "FEATURE_OPEN_DISCUSSIONS_POST_UI": "True",
    "FEATURE_OPEN_DISCUSSIONS_USER_SYNC": "True",
    "FEATURE_OPEN_DISCUSSIONS_USER_UPDATE": "False",
    "FEATURE_PROGRAM_RECORD_LINK": "True",
    "MICROMASTERS_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MICROMASTERS_DB_CONN_MAX_AGE": 0,
    "MICROMASTERS_DB_DISABLE_SSL": "True",
    "MICROMASTERS_ECOMMERCE_EMAIL": "cuddle-bunnies@mit.edu",
    "MICROMASTERS_EMAIL_PORT": 587,
    "MICROMASTERS_EMAIL_TLS": "True",
    "MICROMASTERS_FROM_EMAIL": "MITx MicroMasters <micromasters-support@mit.edu>",
    "MICROMASTERS_SUPPORT_EMAIL": "micromasters-support@mit.edu",
    "MICROMASTERS_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OPENSEARCH_INDEX": "micromasters",
    "OPEN_DISCUSSIONS_REDIRECT_COMPLETE_URL": "/complete/micromasters",
    "OPEN_DISCUSSIONS_SITE_KEY": "micromasters",
    "OPEN_EXCHANGE_RATES_URL": "https://openexchangerates.org/api/",
    "SENTRY_ORG_NAME": "mit-office-of-digital-learning",
    "SENTRY_PROJECT_NAME": "micromasters",
    "UWSGI_PROCESSES": 2,
    "UWSGI_SOCKET_TIMEOUT": 1,
    "UWSGI_THREADS": 50,
}

# Combine var source above with values explicitly defined in pulumi configuration file
heroku_vars.update(**heroku_app_config.get_object("vars"))

auth_aws_mitx_micromasters = vault.generic.get_secret_output(
    path="aws-mitx/creds/micromasters",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)

postgres_micromasters_creds_app = vault.generic.get_secret_output(
    path="postgres-micromasters/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_edx = vault.generic.get_secret_output(
    path="secret-micromasters/edx",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_google = vault.generic.get_secret_output(
    path="secret-micromasters/google",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_mitxonline = vault.generic.get_secret_output(
    path="secret-micromasters/mitxonline",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_opensearch = vault.generic.get_secret_output(
    path="secret-micromasters/opensearch",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_open_discussions = vault.generic.get_secret_output(
    path="secret-micromasters/open-discussions",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_open_exchange_rates = vault.generic.get_secret_output(
    path="secret-micromasters/open-exchange-rates",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_django = vault.generic.get_secret_output(
    path="secret-micromasters/django",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_sentry = vault.generic.get_secret_output(
    path="secret-micromasters/sentry",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_micromasters_cybersource = vault.generic.get_secret_output(
    path="secret-micromasters/cybersource",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_mailgun = vault.generic.get_secret_output(
    path="secret-operations/mailgun",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)
secret_operations_mit_smtp = vault.generic.get_secret_output(
    path="secret-operations/mit-smtp",
    opts=InvokeOptions(parent=micromasters_vault_backend_role),
)

sensitive_heroku_vars = {
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_micromasters.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_micromasters.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "CYBERSOURCE_ACCESS_KEY": secret_micromasters_cybersource.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "CYBERSOURCE_PROFILE_ID": secret_micromasters_cybersource.data.apply(
        lambda data: "{}".format(data["profile_id"])
    ),
    "CYBERSOURCE_SECURITY_KEY": secret_micromasters_cybersource.data.apply(
        lambda data: "{}".format(data["security_key"])
    ),
    "DATABASE_URL": postgres_micromasters_creds_app.data.apply(
        lambda data: (
            "postgres://{}:{}@micromasters-{}-app-db.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/micromasters".format(
                data["username"], data["password"], stack_info.env_suffix.lower()
            )
        )
    ),
    "MAILGUN_KEY": secret_mailgun.data.apply(lambda data: "{}".format(data["api_key"])),
    "MICROMASTERS_EMAIL_HOST": secret_operations_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_host"])
    ),
    "MICROMASTERS_EMAIL_PASSWORD": secret_operations_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_password"])
    ),
    "MICROMASTERS_EMAIL_USER": secret_operations_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_username"])
    ),
    "EDXORG_CLIENT_ID": secret_micromasters_edx.data.apply(
        lambda data: "{}".format(data["client_id"])
    ),
    "EDXORG_CLIENT_SECRET": secret_micromasters_edx.data.apply(
        lambda data: "{}".format(data["client_secret"])
    ),
    "GOOGLE_API_KEY": secret_micromasters_google.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "MITXONLINE_CLIENT_ID": secret_micromasters_mitxonline.data.apply(
        lambda data: "{}".format(data["oauth_client_id"])
    ),
    "MITXONLINE_CLIENT_SECRET": secret_micromasters_mitxonline.data.apply(
        lambda data: "{}".format(data["oauth_client_secret"])
    ),
    "MITXONLINE_STAFF_ACCESS_TOKEN": secret_micromasters_mitxonline.data.apply(
        lambda data: "{}".format(data["staff_access_token"])
    ),
    "OPENSEARCH_HTTP_AUTH": secret_micromasters_opensearch.data.apply(
        lambda data: "{}".format(data["http_auth"])
    ),
    "OPENSEARCH_URL": secret_micromasters_opensearch.data.apply(
        lambda data: "{}".format(data["url"])
    ),
    "OPEN_DISCUSSIONS_JWT_SECRET": secret_micromasters_open_discussions.data.apply(
        lambda data: "{}".format(data["jwt_secret"])
    ),
    "OPEN_EXCHANGE_RATES_APP_ID": secret_micromasters_open_exchange_rates.data.apply(
        lambda data: "{}".format(data["app_id"])
    ),
    "SENTRY_AUTH_TOKEN": secret_micromasters_sentry.data.apply(
        lambda data: "{}".format(data["auth_token"])
    ),
    "SENTRY_DSN": secret_micromasters_sentry.data.apply(
        lambda data: "{}".format(data["dsn"])
    ),
}
if stack_info.env_suffix == "production":
    sensitive_heroku_vars["SECRET_KEY"] = secret_micromasters_django.data.apply(
        lambda data: "{}".format(data["secret_key"])
    )
    sensitive_heroku_vars["STATUS_TOKEN"] = secret_micromasters_django.data.apply(
        lambda data: "{}".format(data["status_token"])
    )

heroku_app_id = heroku_config.require("app_id")
micromasters_heroku_configassociation = heroku.app.ConfigAssociation(
    f"micromasters-{stack_info.env_suffix}-heroku-configassociation",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)


export("micromasters_app", {"rds_host": micromasters_db.db_instance.address})
