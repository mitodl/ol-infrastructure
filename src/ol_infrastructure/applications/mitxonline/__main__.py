# ruff: noqa: E501

"""Create the infrastructure and services needed to support the MITx Online application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
import os
from pathlib import Path

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference, export
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT, DEFAULT_REDIS_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.mitxonline.k8s_secrets import (
    create_mitxonline_k8s_secrets,
)
from ol_infrastructure.components.aws.cache import (
    OLAmazonCache,
    OLAmazonRedisConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider(skip_child_token=True)
setup_heroku_provider()

mitxonline_config = Config("mitxonline")
heroku_config = Config("heroku")
vault_config = Config("vault")

stack_info = parse_stack()
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

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

k8s_global_labels = K8sGlobalLabels(
    service=Services.mitxonline,
    ou=BusinessUnit.mitx_online,
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
mitxonline_namespace = "mitxonline"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(mitxonline_namespace, ns)
)

# Create S3 bucket

# Bucket used to store files from MITx Online app.
mitxonline_bucket_name = f"ol-mitxonline-app-{stack_info.env_suffix}"
mitxonline_bucket = s3.BucketV2(
    f"mitxonline-{stack_info.env_suffix}",
    bucket=mitxonline_bucket_name,
    tags=aws_config.tags,
)
mitxonline_bucket_ownership_controls = s3.BucketOwnershipControls(
    f"mitxonline-{stack_info.env_suffix}-ownership-controls",
    bucket=mitxonline_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
mitxonline_bucket_versioning = s3.BucketVersioningV2(
    f"mitxonline-{stack_info.env_suffix}-versioning",
    bucket=mitxonline_bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
        status="Enabled",
    ),
)
mitxonline_bucket_public_access = s3.BucketPublicAccessBlock(
    f"mitxonline-{stack_info.env_suffix}-public-access-block",
    bucket=mitxonline_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
mitxonline_bucket_policy = s3.BucketPolicy(
    f"mitxonline-{stack_info.env_suffix}-bucket-policy",
    bucket=mitxonline_bucket.id,
    policy=iam.get_policy_document(
        statements=[
            iam.GetPolicyDocumentStatementArgs(
                effect="Allow",
                principals=[
                    iam.GetPolicyDocumentStatementPrincipalArgs(
                        type="AWS",
                        identifiers=["*"],
                    )
                ],
                actions=["s3:GetObject"],
                resources=[mitxonline_bucket.arn.apply("{}/*".format)],
            ),
        ]
    ).json,
)

mitxonline_iam_policy = iam.Policy(
    f"mitxonline-{stack_info.env_suffix}-policy",
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
            },
            "RESOURCE_MISMATCH": {},
        },
    ),
)

mitxonline_vault_backend_role = vault.aws.SecretBackendRole(
    "mitxonline-app",
    name="mitxonline",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
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

db_instance_name = f"mitxonline-{stack_info.env_suffix}-app-db"
mitxonline_db_config = OLPostgresDBConfig(
    instance_name=db_instance_name,
    password=mitxonline_config.require("db_password"),
    subnet_group_name=mitxonline_vpc["rds_subnet"],
    security_groups=[mitxonline_db_security_group],
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="mitxonline",
    public_access=True,
    **db_defaults,
)
mitxonline_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)
mitxonline_db = OLAmazonDB(mitxonline_db_config)
export("mitxonline_app", {"rds_host": mitxonline_db.db_instance.address})

mitxonline_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=mitxonline_db_config.db_name,
    mount_point=f"{mitxonline_db_config.engine}-mitxonline",
    db_admin_username=mitxonline_db_config.username,
    db_admin_password=mitxonline_db_config.password.get_secret_value(),
    db_host=mitxonline_db.db_instance.address,
)
mitxonline_vault_backend = OLVaultDatabaseBackend(mitxonline_vault_backend_config)

env_vars = {
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
    "MITX_ONLINE_FROM_EMAIL": "MITx Online <mitxonline-support@mit.edu>",
    "MITX_ONLINE_OAUTH_PROVIDER": "mitxonline-oauth2",
    "MITX_ONLINE_REPLY_TO_ADDRESS": "MITx Online <mitxonline-support@mit.edu>",
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
    "POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    "POSTHOG_ENABLED": "True",
}
env_vars.update(**mitxonline_config.get_object("vars"))

# All of the secrets for this app must be obtained with async incantations

env_name = (
    stack_info.env_suffix.lower() if stack_info.env_suffix.lower() != "qa" else "rc"
)
openedx_environment = f"mitxonline-{stack_info.env_suffix.lower()}"

# Construct the RDS endpoint string used by both Heroku and K8s secret generation
rds_endpoint = f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:{DEFAULT_POSTGRES_PORT}"

# Vault secrets needed specifically for Heroku deployment
# These are fetched directly during Pulumi execution.
if not mitxonline_config.get_bool("k8s_deploy"):
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

    secret_mitxonline_openedx_retirement_service_worker = (
        vault.generic.get_secret_output(
            path="secret-mitxonline/openedx-retirement-service-worker",
            opts=InvokeOptions(parent=mitxonline_vault_backend_role),
        )
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

    # Combine fetched secrets into the sensitive_env_vars dict for Heroku
    sensitive_env_vars = {
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
            lambda data: "{}".format(data["value"])  # This one stays where it is
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
            lambda data: "{}".format(data["client_id"])  # this one stays
        ),
        "OPENEDX_RETIREMENT_SERVICE_WORKER_CLIENT_SECRET": secret_mitxonline_openedx_retirement_service_worker.data.apply(
            lambda data: "{}".format(data["client_secret"])  # this one stays
        ),
        "OPENEDX_SERVICE_WORKER_API_TOKEN": secret_mitxonline_env_openedx_service_worker_api_token.data.apply(
            lambda data: "{}".format(data["value"])  # this one stays
        ),
        "OPEN_EXCHANGE_RATES_APP_ID": secret_mitxonline_open_exchange_rates.data.apply(
            lambda data: "{}".format(data["app_id"])
        ),
        "POSTHOG_PROJECT_API_KEY": secret_mitxonline_posthog_credentials.data.apply(
            lambda data: "{}".format(data["api-token"])
        ),
        "POSTHOG_API_TOKEN": secret_mitxonline_posthog_credentials.data.apply(
            lambda data: "{}".format(data["api-token"])
        ),  # This one stays where it is
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

    # Configure Heroku app with the fetched secrets
    heroku_app_id = heroku_config.require("app_id")
    mitxonline_heroku_configassociation = heroku.app.ConfigAssociation(
        f"mitxonline-{stack_info.env_suffix}-heroku-configassociation",
        app_id=heroku_app_id,
        sensitive_vars=sensitive_env_vars,
        vars=env_vars,
    )

# Begin k8s resources
if mitxonline_config.get_bool("k8s_deploy"):
    # The mount already exists and is created by hand outside of CI
    # We will create and manage it in CI
    #
    # Once it is imported for the first time we can remove this logic
    if stack_info.env_suffix != "production":
        mount_opts = None
    else:
        mount_opts = ResourceOptions(
            import_="secret-mitxonline", ignore_changes=["options"]
        )
    # TODO (TMM 2025-05-06): The vault mount is also # noqa: TD003, FIX002
    # created/managed as part of the edxapp project. This needs to be factored out into
    # a substructure project or referenced from one stack to the other via stack
    # references. There is some ambiguity about the properl directionality of ownership.
    mitxonline_vault_mount = vault.Mount(
        f"mitxonline-vault-mount-{stack_info.env_suffix}",
        description="Static secrets storage for Open edX {stack_info.env_prefix} applications and services",
        path="secret-mitxonline",
        type="kv",
        opts=mount_opts,
    )
    mitxonline_collected_secrets = read_yaml_secrets(
        Path(f"mitxonline/secrets.{stack_info.env_suffix}.yaml")
    )
    mitxonline_vault_collected_static_secrets = vault.generic.Secret(
        f"mitxonline-collected-static-secrets-{stack_info.env_suffix}",
        path="secret-mitxonline/collected-static-secrets",
        data_json=json.dumps(mitxonline_collected_secrets),
        opts=ResourceOptions(depends_on=[mitxonline_vault_mount]),
    )

    mitxonline_app_security_group = ec2.SecurityGroup(
        f"mitxonline-app-access-{stack_info.env_suffix}",
        description=f"Access control for the MITx Online App in {stack_info.name}",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )

    mitxonline_vault_policy = vault.Policy(
        f"mitxonline-vault-policy-{stack_info.env_suffix}",
        name="mitxonline",
        policy=Path(__file__).parent.joinpath("mitxonline_policy.hcl").read_text(),
    )

    mitxonline_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"mitxonline-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=Services.mitxonline,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[mitxonline_namespace],
        token_policies=[mitxonline_vault_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=Services.mitxonline,
            namespace=mitxonline_namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=mitxonline_vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
        ),
    )

    # Redis / Elasticache
    # only for applications deployed in k8s
    redis_config = Config("redis")
    redis_cluster_security_group = ec2.SecurityGroup(
        f"mitxonline-redis-cluster-security-group-{stack_info.env_suffix}",
        name_prefix=f"mitxonline-redis-cluster-security-group-{stack_info.env_suffix}",
        description="Access control for the mitxonline redis cluster.",
        ingress=[
            ec2.SecurityGroupIngressArgs(
                security_groups=[mitxonline_app_security_group.id],
                protocol="tcp",
                from_port=DEFAULT_REDIS_PORT,
                to_port=DEFAULT_REDIS_PORT,
                description="Allow application pods to talk to Redis",
            ),
        ],
        vpc_id=apps_vpc["id"],
        tags=aws_config.tags,
    )
    redis_cache_config = OLAmazonRedisConfig(
        encrypt_transit=True,
        auth_token=redis_config.require("password"),
        cluster_mode_enabled=False,
        encrypted=True,
        engine_version="7.1",
        num_instances=3,
        shard_count=1,
        auto_upgrade=True,
        cluster_description="Redis cluster for MITxonline",
        cluster_name=f"mitxonline-app-redis-{stack_info.env_suffix}",
        subnet_group=apps_vpc["elasticache_subnet"],
        security_groups=[redis_cluster_security_group.id],
        tags=aws_config.tags,
        **defaults(stack_info)["redis"],
    )
    redis_cache = OLAmazonCache(redis_cache_config)

    # Create Kubernetes secrets using the dedicated function
    # The function returns the names of the secrets and the Pulumi resource objects
    secret_names, secret_resources = create_mitxonline_k8s_secrets(
        stack_info=stack_info,
        mitxonline_namespace=mitxonline_namespace,
        k8s_global_labels=k8s_global_labels,
        vault_k8s_resources=vault_k8s_resources,
        db_config=mitxonline_vault_backend,  # Pass the Vault DB backend config
        rds_endpoint=rds_endpoint,
        openedx_environment=openedx_environment,
        redis_password=redis_config.require("password"),
        redis_cache=redis_cache,
    )

    if "MITXONLINE_DOCKER_TAG" not in os.environ:
        msg = "MITXONLINE_DOCKER_TAG must be set."
        raise OSError(msg)
    MITXONLINE_DOCKER_TAG = os.environ["MITXONLINE_DOCKER_TAG"]

    mitxonline_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=OLApplicationK8sConfig(
            project_root=Path(__file__).parent,
            application_config=env_vars,
            application_name=Services.mitxonline,
            application_namespace=mitxonline_namespace,
            application_lb_service_name="mitxonline-webapp",
            application_lb_service_port_name="http",
            k8s_global_labels=k8s_global_labels,
            # Use the secret names returned by create_mitxonline_k8s_secrets
            env_from_secret_names=secret_names,
            application_security_group_id=mitxonline_app_security_group.id,
            application_security_group_name=mitxonline_app_security_group.name,
            application_image_repository="mitodl/mitxonline-app",
            application_docker_tag=MITXONLINE_DOCKER_TAG,
            application_cmd_array=[
                "uwsgi",
                "/tmp/uwsgi.ini",  # noqa: S108
            ],
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            import_nginx_config=True,
            import_uwsgi_config=True,
            resource_requests={"cpu": "500m", "memory": "512Mi"},
            resource_limits={"cpu": "1000m", "memory": "1024Mi"},
            init_migrations=True,
            init_collectstatic=True,
            celery_worker_configs=[
                OLApplicationK8sCeleryWorkerConfig(
                    worker_name="default",
                    queues=["default"],
                    resource_requests={"cpu": "500m", "memory": "1024Mi"},
                    resource_limits={"cpu": "1000m", "memory": "2048Mi"},
                ),
            ],
        ),
        opts=ResourceOptions(
            # Ensure secrets are created before the application deployment
            depends_on=[mitxonline_app_security_group, *secret_resources]
        ),
    )

    mitxonline_k8s_app_oidc_resources = OLApisixOIDCResources(
        f"mitxonline-k8s-olapisixoidcreources-{stack_info.env_suffix}",
        oidc_config=OLApisixOIDCConfig(
            application_name="mitxonline",
            k8s_labels=k8s_global_labels,
            k8s_namespace=mitxonline_namespace,
            oidc_logout_path="/logout",
            oidc_post_logout_redirect_uri=f"https://{mitxonline_config.require('domain')}/logout",
            oidc_session_cookie_lifetime=60 * 20160,
            oidc_use_session_secret=True,
            vault_mount="secret-operations",
            vault_mount_type="kv-v1",
            vault_path="sso/mitxonline",
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )
    frontend_tls_secret_name = "mitxonline-tls-pair"  # noqa: S105  # pragma: allowlist secret
    cert_manager_certificate = OLCertManagerCert(
        f"mitxonline-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="mitxonline",
            k8s_namespace=mitxonline_namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=frontend_tls_secret_name,
            dns_names=[mitxonline_config.require("domain")],
        ),
    )
    mitxonline_apisix_route_prefix = OLApisixRoute(
        name=f"mitxonline-vault-mount-{stack_info.env_suffix}",
        k8s_namespace=mitxonline_namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixRouteConfig(
                route_name="app-wildcard",
                priority=10,
                hosts=[mitxonline_config.require("domain")],
                paths=["/*"],
                plugins=[
                    mitxonline_k8s_app_oidc_resources.get_full_oidc_plugin_config(
                        unauth_action="pass"
                    ),
                ],
                backend_service_name=mitxonline_k8s_app.application_lb_service_name,
                backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
            ),
            OLApisixRouteConfig(
                route_name="app-login",
                priority=0,
                hosts=[mitxonline_config.require("domain")],
                paths=["/login*"],
                plugins=[
                    mitxonline_k8s_app_oidc_resources.get_full_oidc_plugin_config(
                        unauth_action="auth"
                    ),
                    OLApisixPluginConfig(
                        name="redirect",
                        config={"uri": "/dashboard"},
                    ),
                ],
                backend_service_name=mitxonline_k8s_app.application_lb_service_name,
                backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
            ),
            OLApisixRouteConfig(
                route_name="logout-redirect",
                priority=10,
                hosts=[mitxonline_config.require("domain")],
                paths=["/logout/*"],
                plugins=[
                    OLApisixPluginConfig(
                        name="redirect",
                        config={"uri": "/logout"},
                    ),
                ],
                backend_service_name=mitxonline_k8s_app.application_lb_service_name,
                backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
            ),
        ],
        opts=ResourceOptions(
            delete_before_replace=True,
        ),
    )
