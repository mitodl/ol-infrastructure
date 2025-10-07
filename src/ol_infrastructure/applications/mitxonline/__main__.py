# ruff: noqa: E501

"""Create the infrastructure and services needed to support the MITx Online application.

- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Alias, Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2, iam, s3

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    DEFAULT_WSGI_PORT,
)
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
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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

mitxonline_config = Config("mitxonline")
vault_config = Config("vault")

stack_info = parse_stack()
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
cluster_substructure_stack = StackReference(
    f"substructure.aws.eks.applications.{stack_info.name}"
)
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
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
mitxonline_bucket = s3.Bucket(
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
mitxonline_bucket_versioning = s3.BucketVersioning(
    f"mitxonline-{stack_info.env_suffix}-versioning",
    bucket=mitxonline_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
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
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"yymitxonline-{stack_info.env_suffix}-policy",
            )
        ]
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

mitxonline_app_security_group = ec2.SecurityGroup(
    f"mitxonline-app-access-{stack_info.env_suffix}",
    description=f"Access control for the MITx Online App in {stack_info.name}",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    tags=aws_config.tags,
    vpc_id=apps_vpc["id"],
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
            security_groups=[
                mitxonline_app_security_group.id,
                data_vpc["security_groups"]["orchestrator"],
                data_vpc["security_groups"]["integrator"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            # Airbyte isn't using pod security groups in Kubernetes. This is a
            # workaround to allow for data integration from the data Kubernetes
            # cluster. (TMM 2025-05-16)
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                lambda pod_cidrs: [*pod_cidrs]
            ),
            description="Allow access from the app running in Kubernetes",
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
        {"Name": f"mitxonline-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

db_defaults = {**defaults(stack_info)["rds"]}
if stack_info.name == "QA":
    db_defaults["instance_size"] = DBInstanceTypes.general_purpose_large
if stack_info.name == "Production":
    db_defaults["instance_size"] = DBInstanceTypes.general_purpose_xlarge

db_instance_name = f"mitxonline-{stack_info.env_suffix}-app-db"
mitxonline_db_config = OLPostgresDBConfig(
    instance_name=db_instance_name,
    password=mitxonline_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[mitxonline_db_security_group],
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="mitxonline",
    public_access=False,
    blue_green_timeout_minutes=60 * 6,  # 6 hours
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

env_vars = {
    "CRON_COURSERUN_SYNC_HOURS": "*",
    "FEATURE_IGNORE_EDX_FAILURES": "True",
    "FEATURE_SYNC_ON_DASHBOARD_LOAD": "True",
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
    "MITX_ONLINE_SUPPORT_EMAIL": "mitxonline-support@mit.edu",
    "MITX_ONLINE_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OPENEDX_SERVICE_WORKER_USERNAME": "login_service_user",
    "OPEN_EXCHANGE_RATES_URL": "https://openexchangerates.org/api/",
    "POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    "POSTHOG_ENABLED": "True",
    "SITE_NAME": "MITx Online",
    "USE_X_FORWARDED_HOST": "True",
    "ZENDESK_HELP_WIDGET_ENABLED": "True",
}
env_vars.update(**mitxonline_config.get_object("vars"))

# All of the secrets for this app must be obtained with async incantations

env_name = (
    stack_info.env_suffix.lower() if stack_info.env_suffix.lower() != "qa" else "rc"
)
openedx_environment = f"mitxonline-{stack_info.env_suffix.lower()}"

# Construct the RDS endpoint string used by both Heroku and K8s secret generation
rds_endpoint = f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:{DEFAULT_POSTGRES_PORT}"

# Begin k8s resources
# TODO (TMM 2025-05-06): The vault mount is also # noqa: FIX002
# created/managed as part of the edxapp project. This needs to be factored out into
# a substructure project or referenced from one stack to the other via stack
# references. There is some ambiguity about the properl directionality of ownership.
mitxonline_vault_mount = vault.Mount(
    f"mitxonline-vault-mount-{stack_info.env_suffix}",
    description="Static secrets storage for Open edX {stack_info.env_prefix} applications and services",
    path="secret-mitxonline",
    type="kv",
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
            security_groups=[
                mitxonline_app_security_group.id,
                operations_vpc["security_groups"]["celery_monitoring"],
                cluster_substructure_stack.require_output(
                    "cluster_keda_security_group_id"
                ),
            ],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow application pods to talk to Redis",
        ),
    ],
    vpc_id=apps_vpc["id"],
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
    cluster_description="Redis cluster for MITxonline",
    cluster_name=f"mitxonline-app-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
    **redis_defaults,
)
redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"mitxonline-app-redis-{stack_info.env_suffix}-redis-elasticache-cluster"
            )
        ]
    ),
)

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
if mitxonline_config.get_bool("use_granian"):
    cmd_array = [
        "granian",
    ]
    arg_array = [
        "--interface",
        "wsgi",
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        f"{DEFAULT_WSGI_PORT}",
        "--workers",
        "1",
        "--runtime-threads",
        "2",
        "--log-level",
        "warning",
        "main.wsgi:application",
    ]
else:
    cmd_array = ["uwsgi"]
    arg_array = ["/tmp/uswgi.ini"]  # noqa: S108

mitxonline_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=env_vars,
        application_name=Services.mitxonline,
        application_namespace=mitxonline_namespace,
        application_lb_service_name="mitxonline-webapp",
        application_lb_service_port_name="http",
        application_min_replicas=mitxonline_config.get_int("min_replicas") or 2,
        k8s_global_labels=k8s_global_labels,
        # Use the secret names returned by create_mitxonline_k8s_secrets
        env_from_secret_names=secret_names,
        application_security_group_id=mitxonline_app_security_group.id,
        application_security_group_name=mitxonline_app_security_group.name,
        application_image_repository="mitodl/mitxonline-app",
        application_docker_tag=MITXONLINE_DOCKER_TAG,
        application_cmd_array=cmd_array,
        application_arg_array=arg_array,
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        import_nginx_config=True,
        import_uwsgi_config=True,
        init_migrations=False,
        init_collectstatic=True,
        pre_deploy_commands=[
            ("migrate", ["python", "manage.py", "migrate", "--noinput"])
        ],
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="celery",
                redis_host=redis_cache.address,
                redis_password=redis_config.require("password"),
                resource_requests={"cpu": "500m", "memory": "2Gi"},
            ),
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="hubspot_sync",
                redis_host=redis_cache.address,
                redis_password=redis_config.require("password"),
            ),
        ],
        resource_requests={"cpu": "500m", "memory": "1800Mi"},
        resource_limits={"cpu": "1000m", "memory": "1800Mi"},
        hpa_scaling_metrics=[
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="cpu",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=60,  # Target CPU utilization (60%)
                    ),
                ),
            ),
            # Scale up when avg usage exceeds: 1800 * 0.8 = 1440 Mi
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="memory",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=80,  # Target memory utilization (80%)
                    ),
                ),
            ),
        ],
    ),
    opts=ResourceOptions(
        # Ensure secrets are created before the application deployment
        depends_on=[mitxonline_app_security_group, *secret_resources]
    ),
)
api_domain = mitxonline_config.require("domain")
api_path_prefix = "mitxonline"
frontend_tls_secret_name = "mitxonline-tls-pair"  # noqa: S105  # pragma: allowlist secret
cert_manager_certificate = OLCertManagerCert(
    f"mitxonline-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="mitxonline",
        k8s_namespace=mitxonline_namespace,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        dest_secret_name=frontend_tls_secret_name,
        dns_names=[api_domain],
    ),
)
application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "mitxonline",
    "ol.mit.edu/pod-security-group": "mitxonline",
}
mitxonline_direct_oidc = OLApisixOIDCResources(
    f"ol-mitxonline-k8s-olapisixoidcresources-no-prefix-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="mitxonline-k8s-no-prefix",
        k8s_labels=application_labels,
        k8s_namespace=mitxonline_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{api_domain}/logout/",
        oidc_session_cookie_lifetime=60 * 20160,
        oidc_session_cookie_domain=api_domain.removeprefix("api"),
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)
mitxonline_prefixed_oidc_resources = OLApisixOIDCResources(
    f"ol-mitxonline-k8s-olapisixoidcresources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="mitxonline-k8s",
        k8s_labels=application_labels,
        k8s_namespace=mitxonline_namespace,
        oidc_logout_path=f"/{api_path_prefix}/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{api_domain}/{api_path_prefix}/logout/",
        oidc_session_cookie_lifetime=60 * 20160,
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)
mitxonline_shared_plugins = OLApisixSharedPlugins(
    name="ol-mitxonline-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="mitxonline",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=mitxonline_namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

proxy_rewrite_plugin_config = OLApisixPluginConfig(
    name="proxy-rewrite",
    config={
        "regex_uri": [
            f"/{api_path_prefix}/(.*)",
            "/$1",
        ],
    },
)

response_rewrite_plugin_config = OLApisixPluginConfig(
    name="response-rewrite",
    config={
        "headers": {
            "set": {
                "Content-Security-Policy": f"frame-ancestors 'self' {env_vars['OPENEDX_API_BASE_URL']}"
            }
        }
    },
)
mitxonline_apisix_route_direct = OLApisixRoute(
    name=f"mitxonline-apisix-route-direct-{stack_info.env_suffix}",
    k8s_namespace=mitxonline_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            hosts=[api_domain],
            paths=["/*"],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            plugins=[
                mitxonline_direct_oidc.get_full_oidc_plugin_config(
                    unauth_action="pass"
                ),
                response_rewrite_plugin_config,
            ],
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            hosts=[api_domain],
            paths=["/logout/oidc/*"],
            plugins=[
                OLApisixPluginConfig(name="redirect", config={"uri": "/logout/oidc"}),
                response_rewrite_plugin_config,
            ],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            hosts=[api_domain],
            paths=["/login/", "/admin/login/*", "/login", "/login/oidc*"],
            plugins=[
                mitxonline_direct_oidc.get_full_oidc_plugin_config(
                    unauth_action="auth"
                ),
                response_rewrite_plugin_config,
            ],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

learn_api_domain = mitxonline_config.require("learn_backend_domain")  # New domain

mitxonline_apisix_route_prefix = OLApisixRoute(
    name=f"mitxonline-apisix-route-prefixed-{stack_info.env_suffix}",
    k8s_namespace=mitxonline_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            hosts=[learn_api_domain],
            paths=[f"/{api_path_prefix}/*"],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                mitxonline_prefixed_oidc_resources.get_full_oidc_plugin_config(
                    unauth_action="pass"
                ),
                response_rewrite_plugin_config,
            ],
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            hosts=[learn_api_domain],
            paths=[f"/{api_path_prefix}/logout/oidc/*"],
            plugins=[
                OLApisixPluginConfig(name="redirect", config={"uri": "/logout/oidc"}),
                response_rewrite_plugin_config,
            ],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            hosts=[learn_api_domain],
            paths=[
                f"/{api_path_prefix}/login/",
                f"/{api_path_prefix}/login/oidc*",
                f"/{api_path_prefix}/admin/login/*",
                f"/{api_path_prefix}/login",
            ],
            plugins=[
                proxy_rewrite_plugin_config,
                mitxonline_prefixed_oidc_resources.get_full_oidc_plugin_config(
                    unauth_action="auth"
                ),
                response_rewrite_plugin_config,
            ],
            shared_plugin_config_name=mitxonline_shared_plugins.resource_name,
            backend_service_name=mitxonline_k8s_app.application_lb_service_name,
            backend_service_port=mitxonline_k8s_app.application_lb_service_port_name,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

export(
    "mitxonline",
    {
        "rds_host": mitxonline_db.db_instance.address,
        "redis": redis_cache.address,
        "redis_token": redis_cache.cache_cluster.auth_token,
    },
)
