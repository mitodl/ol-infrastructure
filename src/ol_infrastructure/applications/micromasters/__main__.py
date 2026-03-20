"""Create the infrastructure and services needed to support the
MicroMasters application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import base64
import json
import mimetypes
import os
import textwrap
from pathlib import Path

import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_NGINX_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
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

micromasters_config = Config("micromasters")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)
micromasters_vpc = network_stack.require_output("applications_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
micromasters_environment = f"micromasters-{stack_info.env_suffix}"

fastly_provider = get_fastly_provider()
ol_zone_id = dns_stack.require_output("odl")["id"]
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
    ownership_controls="BucketOwnerPreferred",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
    bucket_policy_document=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadStaticFiles",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{micromasters_bucket_name}/*",
                }
            ],
        }
    ),
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


if micromasters_config.get_bool("deploy_k8s"):
    if "MICROMASTERS_DOCKER_TAG" not in os.environ:
        msg = "MICROMASTERS_DOCKER_TAG must be set."
        raise OSError(msg)
    MICROMASTERS_DOCKER_TAG = os.environ["MICROMASTERS_DOCKER_TAG"]

    vault_config = Config("vault")
    redis_config = Config("redis")
    cluster_stack = StackReference(
        f"infrastructure.aws.eks.applications.{stack_info.name}"
    )
    cluster_substructure_stack = StackReference(
        f"substructure.aws.eks.applications.{stack_info.name}"
    )

    setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

    micromasters_namespace = "micromasters"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(micromasters_namespace, ns)
    )

    k8s_global_labels = K8sGlobalLabels(
        ou=BusinessUnit.micromasters,
        service=Services.micromasters,
        stack=stack_info,
    ).model_dump()

    k8s_pod_subnet_cidrs = micromasters_vpc["k8s_pod_subnet_cidrs"]

    micromasters_app_security_group = ec2.SecurityGroup(
        f"micromasters-app-{stack_info.env_suffix}",
        description=(
            f"Access control for the MicroMasters app pods in {stack_info.name}"
        ),
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=micromasters_vpc["id"],
    )

    # Redis / Elasticache
    redis_cluster_security_group = ec2.SecurityGroup(
        f"micromasters-redis-cluster-security-group-{stack_info.env_suffix}",
        name_prefix=f"micromasters-redis-cluster-security-group-{stack_info.env_suffix}",
        description="Access control for the MicroMasters redis cluster.",
        ingress=[
            ec2.SecurityGroupIngressArgs(
                security_groups=[
                    micromasters_app_security_group.id,
                    cluster_substructure_stack.require_output(
                        "cluster_keda_security_group_id"
                    ),
                ],
                protocol="tcp",
                from_port=DEFAULT_REDIS_PORT,
                to_port=DEFAULT_REDIS_PORT,
                description="Allow application pods to talk to Redis",
            ),
            ec2.SecurityGroupIngressArgs(
                cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
                protocol="tcp",
                from_port=DEFAULT_REDIS_PORT,
                to_port=DEFAULT_REDIS_PORT,
                description=(
                    "Allow Operations VPC celery monitoring pods to talk to Redis"
                ),
            ),
        ],
        vpc_id=micromasters_vpc["id"],
        tags=aws_config.tags,
    )
    redis_defaults = defaults(stack_info)["redis"]
    redis_defaults["instance_type"] = (
        redis_config.get("instance_type") or redis_defaults["instance_type"]
    )
    redis_cache_config = OLAmazonRedisConfig(
        encrypt_transit=True,
        auth_token=redis_config.require("password"),
        cluster_mode_enabled=False,
        encrypted=True,
        engine_version="7.2",
        engine="valkey",
        num_instances=3,
        shard_count=1,
        auto_upgrade=True,
        cluster_description="Redis cluster for MicroMasters",
        cluster_name=f"micromasters-app-redis-{stack_info.env_suffix}",
        subnet_group=micromasters_vpc["elasticache_subnet"],
        security_groups=[redis_cluster_security_group.id],
        tags=aws_config.tags,
        **redis_defaults,
    )
    redis_cache = OLAmazonCache(redis_cache_config)
    micromasters_vault_k8s_policy = vault.Policy(
        f"micromasters-vault-k8s-policy-{stack_info.env_suffix}",
        name="micromasters",
        policy=Path(__file__).parent.joinpath("micromasters_policy.hcl").read_text(),
    )

    micromasters_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"micromasters-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name="micromasters",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[micromasters_namespace],
        token_policies=[micromasters_vault_k8s_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name="micromasters",
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=micromasters_vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[micromasters_vault_k8s_auth_backend_role],
        ),
    )

    vaultauth = vault_k8s_resources.auth_name
    secret_opts = ResourceOptions(
        delete_before_replace=True,
        depends_on=[vault_k8s_resources],
    )

    # The secret-micromasters Vault mount is kv-v2 in CI but kv-v1 in QA/Production
    secret_micromasters_mount_type = (
        "kv-v2" if stack_info.env_suffix == "ci" else "kv-v1"
    )

    # Dynamic AWS credentials
    aws_creds_secret_name = "micromasters-aws-creds"  # noqa: S105  # pragma: allowlist secret
    aws_creds_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-aws-creds-secret",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name=aws_creds_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=aws_creds_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="aws-mitx",
            path="creds/micromasters",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "AWS_ACCESS_KEY_ID": '{{ get .Secrets "access_key" }}',
                "AWS_SECRET_ACCESS_KEY": '{{ get .Secrets "secret_key" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Dynamic PostgreSQL credentials
    db_instance_name = f"micromasters-{stack_info.env_suffix}-app-db"
    rds_endpoint = f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com"
    db_creds_secret_name = "micromasters-db-creds"  # noqa: S105  # pragma: allowlist secret
    db_creds_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-db-creds-secret",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name=db_creds_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount=micromasters_vault_backend.db_mount.path,
            path="creds/app",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "DATABASE_URL": (
                    'postgres://{{ get .Secrets "username" }}'
                    ':{{ get .Secrets "password" }}'
                    f"@{rds_endpoint}:{DEFAULT_POSTGRES_PORT}/micromasters"
                ),
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - edX credentials
    edx_secret_name = "micromasters-edx-secret"  # noqa: S105  # pragma: allowlist secret
    edx_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-edx-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=edx_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=edx_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="edx",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "EDXORG_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "EDXORG_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Google credentials
    google_secret_name = "micromasters-google-secret"  # noqa: S105  # pragma: allowlist secret
    google_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-google-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=google_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=google_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="google",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "GOOGLE_API_KEY": '{{ get .Secrets "api_key" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - MITx Online credentials
    mitxonline_secret_name = "micromasters-mitxonline-secret"  # noqa: S105  # pragma: allowlist secret
    mitxonline_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-mitxonline-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=mitxonline_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=mitxonline_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="mitxonline",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "MITXONLINE_CLIENT_ID": '{{ get .Secrets "oauth_client_id" }}',
                "MITXONLINE_CLIENT_SECRET": (
                    '{{ get .Secrets "oauth_client_secret" }}'
                ),
                "MITXONLINE_STAFF_ACCESS_TOKEN": (
                    '{{ get .Secrets "staff_access_token" }}'
                ),
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - OpenSearch credentials
    opensearch_secret_name = "micromasters-opensearch-secret"  # noqa: S105  # pragma: allowlist secret
    opensearch_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-opensearch-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=opensearch_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=opensearch_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="opensearch",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "OPENSEARCH_HTTP_AUTH": '{{ get .Secrets "http_auth" }}',
                "OPENSEARCH_URL": '{{ get .Secrets "url" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Open Discussions credentials
    open_discussions_secret_name = "micromasters-open-discussions-secret"  # noqa: S105  # pragma: allowlist secret
    open_discussions_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-open-discussions-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=open_discussions_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=open_discussions_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="open-discussions",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "OPEN_DISCUSSIONS_JWT_SECRET": '{{ get .Secrets "jwt_secret" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Open Exchange Rates credentials
    open_exchange_rates_secret_name = "micromasters-open-exchange-rates-secret"  # noqa: S105  # pragma: allowlist secret
    open_exchange_rates_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-open-exchange-rates-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=open_exchange_rates_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=open_exchange_rates_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="open-exchange-rates",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "OPEN_EXCHANGE_RATES_APP_ID": '{{ get .Secrets "app_id" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Django secrets
    django_secret_name = "micromasters-django-secret"  # noqa: S105  # pragma: allowlist secret
    django_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-django-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=django_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=django_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="django",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "SECRET_KEY": '{{ get .Secrets "secret_key" }}',
                "STATUS_TOKEN": '{{ get .Secrets "status_token" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Sentry credentials
    sentry_secret_name = "micromasters-sentry-secret"  # noqa: S105  # pragma: allowlist secret
    sentry_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-sentry-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=sentry_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=sentry_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="sentry",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "SENTRY_AUTH_TOKEN": '{{ get .Secrets "auth_token" }}',
                "SENTRY_DSN": '{{ get .Secrets "dsn" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - CyberSource credentials
    cybersource_secret_name = "micromasters-cybersource-secret"  # noqa: S105  # pragma: allowlist secret
    cybersource_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-cybersource-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=cybersource_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=cybersource_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-micromasters",
            mount_type=secret_micromasters_mount_type,
            path="cybersource",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "CYBERSOURCE_ACCESS_KEY": '{{ get .Secrets "access_key" }}',
                "CYBERSOURCE_PROFILE_ID": '{{ get .Secrets "profile_id" }}',
                "CYBERSOURCE_SECURITY_KEY": '{{ get .Secrets "security_key" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - Mailgun credentials
    mailgun_secret_name = "micromasters-mailgun-secret"  # noqa: S105  # pragma: allowlist secret
    mailgun_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-mailgun-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=mailgun_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=mailgun_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v1",
            path="mailgun",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "MAILGUN_KEY": '{{ get .Secrets "api_key" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Static secrets - MIT SMTP credentials
    mit_smtp_secret_name = "micromasters-mit-smtp-secret"  # noqa: S105  # pragma: allowlist secret
    mit_smtp_secret = OLVaultK8SSecret(
        f"micromasters-{stack_info.env_suffix}-mit-smtp-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=mit_smtp_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
            dest_secret_name=mit_smtp_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v1",
            path="mit-smtp",
            excludes=[".*"],
            exclude_raw=True,
            templates={
                "MICROMASTERS_EMAIL_HOST": '{{ get .Secrets "relay_host" }}',
                "MICROMASTERS_EMAIL_PASSWORD": '{{ get .Secrets "relay_password" }}',
                "MICROMASTERS_EMAIL_USER": '{{ get .Secrets "relay_username" }}',
            },
            vaultauth=vaultauth,
        ),
        opts=secret_opts,
    )

    # Redis credentials (plain K8s secret)
    redis_creds_secret_name = "micromasters-redis-creds"  # noqa: S105  # pragma: allowlist secret
    redis_creds_secret = kubernetes.core.v1.Secret(
        f"micromasters-{stack_info.env_suffix}-redis-creds",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=redis_creds_secret_name,
            namespace=micromasters_namespace,
            labels=k8s_global_labels,
        ),
        string_data=redis_cache.address.apply(
            lambda address: {
                "REDIS_URL": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}",
                "CELERY_BROKER_URL": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
                "CELERY_RESULT_BACKEND": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
            }
        ),
        opts=ResourceOptions(
            depends_on=[redis_cache],
            delete_before_replace=True,
        ),
    )

    k8s_secret_names = [
        aws_creds_secret_name,
        db_creds_secret_name,
        edx_secret_name,
        google_secret_name,
        mitxonline_secret_name,
        opensearch_secret_name,
        open_discussions_secret_name,
        open_exchange_rates_secret_name,
        django_secret_name,
        sentry_secret_name,
        cybersource_secret_name,
        mailgun_secret_name,
        mit_smtp_secret_name,
        redis_creds_secret_name,
    ]
    k8s_secret_resources = [
        aws_creds_secret,
        db_creds_secret,
        edx_secret,
        google_secret,
        mitxonline_secret,
        opensearch_secret,
        open_discussions_secret,
        open_exchange_rates_secret,
        django_secret,
        sentry_secret,
        cybersource_secret,
        mailgun_secret,
        mit_smtp_secret,
        redis_creds_secret,
    ]

    k8s_env_vars: dict[str, str | int] = {
        "BATCH_UPDATE_RATE_LIMIT": "2/m",
        "CLIENT_ELASTICSEARCH_URL": "/api/v0/search/",
        "CRONTAB_DISCUSSIONS_SYNC": "0 9 * * *",
        "FEATURE_ENABLE_PROGRAM_LETTER": "True",
        "FEATURE_FINAL_GRADE_ALGORITHM": "v1",
        "FEATURE_MITXONLINE_LOGIN": "True",
        "FEATURE_OPEN_DISCUSSIONS_CREATE_CHANNEL_UI": "True",
        "FEATURE_OPEN_DISCUSSIONS_POST_UI": "True",
        "FEATURE_OPEN_DISCUSSIONS_USER_SYNC": "True",
        "FEATURE_OPEN_DISCUSSIONS_USER_UPDATE": "False",
        "FEATURE_PROGRAM_RECORD_LINK": "True",
        "MICROMASTERS_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
        "MICROMASTERS_DB_CONN_MAX_AGE": "0",
        "MICROMASTERS_ECOMMERCE_EMAIL": "cuddle-bunnies@mit.edu",
        "MICROMASTERS_EMAIL_PORT": "587",
        "MICROMASTERS_EMAIL_TLS": "True",
        "MICROMASTERS_FROM_EMAIL": "MITx MicroMasters <micromasters-support@mit.edu>",
        "MICROMASTERS_SUPPORT_EMAIL": "micromasters-support@mit.edu",
        "MICROMASTERS_USE_S3": "True",
        "OPENSEARCH_INDEX": "micromasters",
        "OPEN_DISCUSSIONS_REDIRECT_COMPLETE_URL": "/complete/micromasters",
        "OPEN_DISCUSSIONS_SITE_KEY": "micromasters",
        "OPEN_EXCHANGE_RATES_URL": "https://openexchangerates.org/api/",
        "SENTRY_ORG_NAME": "mit-office-of-digital-learning",
        "SENTRY_PROJECT_NAME": "micromasters",
        "USE_X_FORWARDED_HOST": "True",
    }
    k8s_env_vars.update(micromasters_config.get_object("vars") or {})

    micromasters_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=OLApplicationK8sConfig(
            project_root=Path(__file__).parent,
            application_config=k8s_env_vars,
            application_name=Services.micromasters,
            application_namespace=micromasters_namespace,
            application_lb_service_name="micromasters-webapp",
            application_lb_service_port_name="http",
            application_min_replicas=micromasters_config.get_int("min_replicas") or 2,
            k8s_global_labels=k8s_global_labels,
            env_from_secret_names=k8s_secret_names,
            application_security_group_id=micromasters_app_security_group.id,
            application_security_group_name=micromasters_app_security_group.name,
            application_image_repository="mitodl/micromasters-app",
            application_docker_tag=MICROMASTERS_DOCKER_TAG,
            application_cmd_array=["uwsgi"],
            application_arg_array=["/tmp/uwsgi.ini"],  # noqa: S108
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            import_nginx_config=True,
            import_nginx_config_path="files/web.conf_uwsgi",
            import_uwsgi_config=True,
            init_migrations=False,
            init_collectstatic=True,
            resource_requests={"cpu": "250m", "memory": "1500Mi"},
            resource_limits={"memory": "1500Mi"},
            celery_worker_configs=[
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="micromasters.celery:app",
                    queue_name="search",
                    queues=["search"],
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="micromasters.celery:app",
                    queue_name="exams",
                    queues=["exams"],
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="micromasters.celery:app",
                    queue_name="dashboard",
                    queues=["dashboard"],
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="micromasters.celery:app",
                    queue_name="default",
                    queues=["default"],
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
            ],
            probe_configs={
                "liveness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health", port=DEFAULT_NGINX_PORT
                    ),
                    initial_delay_seconds=30,
                    period_seconds=30,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "readiness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=15,
                    period_seconds=15,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "startup_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=10,
                    period_seconds=10,
                    failure_threshold=6,
                    success_threshold=1,
                    timeout_seconds=5,
                ),
            },
        ),
        opts=ResourceOptions(
            depends_on=[micromasters_app_security_group, *k8s_secret_resources]
        ),
    )
    # APISIX routing (pass-through, no OIDC)
    backend_domain = micromasters_config.require("backend_domain")
    frontend_domain = micromasters_config.require("frontend_domain")
    tls_secret_name = "micromasters-tls-pair"  # noqa: S105  # pragma: allowlist secret

    cert_manager_certificate = OLCertManagerCert(
        f"micromasters-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="micromasters",
            k8s_namespace=micromasters_namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[backend_domain],
        ),
    )

    micromasters_apisix_httproute = OLApisixHTTPRoute(
        f"micromasters-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="passthrough",
                hosts=[backend_domain],
                paths=["/*"],
                backend_service_name=micromasters_k8s_app.application_lb_service_name,
                backend_service_port=micromasters_k8s_app.application_lb_service_port_name,
                plugins=[],
            ),
        ],
        k8s_namespace=micromasters_namespace,
        k8s_labels=k8s_global_labels,
    )

    ################################################
    # Fastly CDN configuration
    vector_log_proxy_secrets = read_yaml_secrets(
        Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
    )
    fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
    encoded_fastly_proxy_credentials = base64.b64encode(
        f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
    ).decode("utf8")
    vector_log_proxy_domain = vector_log_proxy_stack.require_output(
        "vector_log_proxy_domain"
    )

    gzip_settings: dict[str, set[str]] = {"extensions": set(), "content_types": set()}
    for k, v in mimetypes.types_map.items():
        if k in (
            ".json",
            ".pdf",
            ".js",
            ".css",
            ".html",
            ".xml",
            ".svg",
            ".txt",
            ".csv",
        ):
            gzip_settings["extensions"].add(k.strip("."))
            gzip_settings["content_types"].add(v)

    micromasters_fastly_service = fastly.ServiceVcl(
        f"micromasters-fastly-service-{stack_info.env_suffix}",
        name=f"MicroMasters {stack_info.env_suffix}",
        comment="Managed by Pulumi",
        backends=[
            fastly.ServiceVclBackendArgs(
                address=backend_domain,
                name="micromasters_backend",
                override_host=backend_domain,
                port=DEFAULT_HTTPS_PORT,
                ssl_cert_hostname=backend_domain,
                ssl_sni_hostname=backend_domain,
                use_ssl=True,
            ),
        ],
        gzips=[
            fastly.ServiceVclGzipArgs(
                name="enable-gzip-compression",
                extensions=sorted(gzip_settings["extensions"]),
                content_types=sorted(gzip_settings["content_types"]),
            )
        ],
        product_enablement=fastly.ServiceVclProductEnablementArgs(
            brotli_compression=True,
        ),
        cache_settings=[],
        conditions=[],
        dictionaries=[],
        domains=[
            fastly.ServiceVclDomainArgs(
                comment=f"MicroMasters {stack_info.env_prefix} {stack_info.env_suffix}",
                name=frontend_domain,
            ),
        ],
        request_settings=[
            fastly.ServiceVclRequestSettingArgs(
                force_ssl=True,
                name="Generated by force TLS and enable HSTS",
                xff="",
            )
        ],
        headers=[
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Strict-Transport-Security",
                name="Generated by force TLS and enable HSTS",
                source='"max-age=300"',
                type="response",
            ),
        ],
        snippets=[
            fastly.ServiceVclSnippetArgs(
                name="Redirect to correct domain",
                content=textwrap.dedent(
                    rf"""
                    # redirect to the correct host/domain
                    if (obj.status == 618 && obj.response == "redirect-host") {{
                      set obj.status = 302;
                      set obj.http.Location = "https://"
                        + "{frontend_domain}"
                        + req.url.path
                        + if (std.strlen(req.url.qs) > 0, "?" req.url.qs, "");
                      return (deliver);
                    }}
                    """
                ),
                type="error",
            ),
        ],
        logging_https=[
            fastly.ServiceVclLoggingHttpArgs(
                url=Output.all(domain=vector_log_proxy_domain).apply(
                    lambda kwargs: f"https://{kwargs['domain']}/fastly"
                ),
                name=f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}-https-logging-args",
                content_type="application/json",
                format=build_fastly_log_format_string(additional_static_fields={}),
                format_version=2,
                header_name="Authorization",
                header_value=f"Basic {encoded_fastly_proxy_credentials}",
                json_format="0",
                method="POST",
                request_max_bytes=ONE_MEGABYTE_BYTE,
            )
        ],
        opts=ResourceOptions.merge(fastly_provider, ResourceOptions()),
    )

    # Point the frontend domain at Fastly
    five_minutes = 60 * 5
    route53.Record(
        f"micromasters-frontend-dns-{stack_info.env_suffix}",
        name=frontend_domain,
        allow_overwrite=True,
        type="A",
        ttl=five_minutes,
        records=[str(addr) for addr in FASTLY_A_TLS_1_3],
        zone_id=ol_zone_id,
        opts=ResourceOptions(delete_before_replace=True),
    )


export("micromasters_app", {"rds_host": micromasters_db.db_instance.address})
