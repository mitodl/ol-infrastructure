"""Deploy Label Studio Community on the data EKS cluster for courseware annotation.

The courseware gold-set tooling in ol-data-platform
(``ml.lib.courseware.evaluation.labelstudio``) writes Label Studio task imports
and labeling configs and reads Label Studio JSON exports. This stack runs the
instance those files go through.

Label Studio Community has no SSO (that is an Enterprise feature), so APISIX
gates every route with Keycloak OIDC against the ol-data-platform realm, and
annotators then sign in with local Label Studio accounts behind it. Signup is
restricted to the organization invite link, so the gateway decides who can
reach the instance and an admin decides who gets an account on it.

State lives outside the cluster: an RDS Postgres database with Vault-issued
credentials, and an S3 bucket for uploaded files and exports, reached with the
pod's IRSA role.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export
from pulumi_aws import ec2

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import LABEL_STUDIO_CHART_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixOIDCVaultCredentials,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultPostgresDatabaseConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    Application,
    AWSBase,
    BusinessUnit,
    Component,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
label_studio_config = Config("label_studio")
vault_config = Config("vault")

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
setup_k8s_provider(require_stack_output_value(cluster_stack, "kube_config"))
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
data_vpc = network_stack.require_output("data_vpc")
vault_infra_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)

NAMESPACE = "label-studio"
RELEASE_NAME = "label-studio"
# The chart names the app Deployment and Service "<release>-ls-app".
APP_NAME = f"{RELEASE_NAME}-ls-app"
APP_SERVICE_PORT = 80
SERVICE_ACCOUNT_NAME = "label-studio"
DB_NAME = "labelstudio"
DB_CREDS_SECRET_NAME = "label-studio-db-creds"  # pragma: allowlist secret  # noqa: S105
ADMIN_SECRET_NAME = "label-studio-admin"  # pragma: allowlist secret  # noqa: S105

domain = label_studio_config.require("domain")
bucket_name = f"ol-data-label-studio-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={"OU": BusinessUnit.data, "Environment": f"data-{stack_info.env_suffix}"}
)
k8s_app_labels = K8sAppLabels(
    application=Application.label_studio,
    product=Product.data,
    service=Services.label_studio,
    ou=BusinessUnit.data,
    source_repository="https://github.com/HumanSignal/label-studio",
    stack=stack_info,
)
k8s_labels = k8s_app_labels.model_dump()

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

################################
# S3 bucket for uploads and exports
################################
# Label Studio's default file storage. Task imports uploaded through the UI and
# export files land here. The courseware tasks carry their text inline in the
# task data, so no browser fetches media from this bucket and it needs no CORS.
label_studio_bucket = OLBucket(
    f"label-studio-bucket-{stack_info.env_suffix}",
    S3BucketConfig(
        bucket_name=bucket_name,
        versioning_enabled=True,
        noncurrent_version_expiration_days=30,
        tags=aws_config.tags,
    ),
)

# Label Studio only sets AWS keys on its S3 storage backend when
# STORAGE_AWS_ACCESS_KEY_ID / STORAGE_AWS_SECRET_ACCESS_KEY are present
# (label_studio/core/settings/base.py), so leaving them unset falls through to
# the default credential chain and the IRSA role below.
label_studio_bucket_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:aws:s3:::{bucket_name}"],
        },
    ],
}

label_studio_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="label-studio",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=label_studio_bucket_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("label_studio_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=SERVICE_ACCOUNT_NAME,
        vault_sync_service_account_names=["label-studio-vault"],
        k8s_labels=k8s_app_labels,
    )
)

################################
# RDS Postgres
################################
label_studio_db_security_group = ec2.SecurityGroup(
    f"label-studio-rds-security-group-{stack_info.env_suffix}",
    name_prefix=f"label-studio-rds-{stack_info.env_suffix}-",
    description="Grant access to the Label Studio database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
            description="Grant access to RDS from EKS pods",
        ),
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[
                vault_infra_stack.require_output("vault_server")["security_group"]
            ],
            description="Grant access to RDS from Vault",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"label-studio-rds-{stack_info.env_suffix}"}),
    vpc_id=data_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = label_studio_config.require("db_instance_size")
rds_defaults["read_replica"] = None
label_studio_db_config = OLPostgresDBConfig(
    instance_name=f"ol-label-studio-db-{stack_info.env_suffix}",
    password=label_studio_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[label_studio_db_security_group],
    tags=aws_config.tags,
    db_name=DB_NAME,
    **rds_defaults,
)
label_studio_db = OLAmazonDB(label_studio_db_config)

label_studio_db_vault_backend = OLVaultDatabaseBackend(
    OLVaultPostgresDatabaseConfig(
        db_name=DB_NAME,
        mount_point=f"{label_studio_db_config.engine}-label-studio",
        db_admin_username=label_studio_db_config.username,
        db_admin_password=label_studio_config.require("db_password"),
        db_host=label_studio_db.db_instance.address,
    ),
    opts=ResourceOptions(delete_before_replace=True, parent=label_studio_db),
)

# creds/app mints a new username as well as a new password on every lease, so
# both are synced. Rotation restarts the Deployment, which re-reads them.
label_studio_db_creds = OLVaultK8SSecret(
    f"label-studio-db-creds-{stack_info.env_suffix}",
    OLVaultK8SDynamicSecretConfig(
        name=DB_CREDS_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_labels,
        dest_secret_labels=k8s_labels,
        dest_secret_name=DB_CREDS_SECRET_NAME,
        mount=label_studio_db_vault_backend.db_mount.path,
        path="creds/app",
        exclude_raw=True,
        templates={
            "DB_USER": "{{ .Secrets.username }}",
            "DB_PASS": "{{ .Secrets.password }}",
        },
        restart_targets=[OLVaultRestartTarget(kind="Deployment", name=APP_NAME)],
        vaultauth=label_studio_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[label_studio_auth_binding.vault_k8s_resources],
    ),
)

################################
# Bootstrap admin
################################
# `label-studio init` runs at every app container start and creates this user
# (superuser, and the organization if none exists) when it does not exist yet.
# It never resets the password of an existing user. With signup restricted to
# the invite link, this account is how the first admin gets in.
label_studio_admin_secret = kubernetes.core.v1.Secret(
    f"label-studio-admin-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ADMIN_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_labels,
    ),
    string_data={
        "username": label_studio_config.require("admin_email"),
        "password": label_studio_config.require_secret("admin_password"),
    },
)

################################
# Helm release
################################
label_studio_release = kubernetes.helm.v3.Release(
    f"label-studio-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name=RELEASE_NAME,
        chart="label-studio",
        version=LABEL_STUDIO_CHART_VERSION,
        namespace=NAMESPACE,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://charts.heartex.com",
        ),
        values={
            "global": {
                "pgConfig": {
                    "host": label_studio_db.db_instance.address,
                    "port": label_studio_db_config.port,
                    "dbName": DB_NAME,
                    # The chart's config check requires a static userName, but
                    # Vault issues a new one per lease. app.extraEnvironmentSecrets
                    # below replaces POSTGRE_USER in the migration and app
                    # containers; only the nginx sidecar sees this value, and it
                    # does not connect to the database.
                    "userName": "set-from-vault",
                    "password": {
                        "secretName": DB_CREDS_SECRET_NAME,
                        "secretKey": "DB_PASS",  # pragma: allowlist secret
                    },
                    # RDS enforces TLS (rds.force_ssl). The entrypoint exports
                    # this as PGSSLMODE for both the wait-for-db check and Django.
                    "ssl": {"pgSslMode": "require"},
                },
                "persistence": {
                    "enabled": True,
                    "type": "s3",
                    "config": {
                        "s3": {
                            "region": aws_config.region,
                            "bucket": label_studio_bucket.bucket_v2.bucket,
                        },
                    },
                },
                "extraEnvironmentVars": {
                    # Without a host, Label Studio builds http:// links.
                    "LABEL_STUDIO_HOST": f"https://{domain}",
                    # APISIX terminates TLS and Label Studio sets no
                    # SECURE_PROXY_SSL_HEADER, so Django sees plain http and its
                    # CSRF origin check rejects the https Origin unless trusted.
                    "CSRF_TRUSTED_ORIGINS": f"https://{domain}",
                    "SESSION_COOKIE_SECURE": "1",
                    "CSRF_COOKIE_SECURE": "1",
                    # New accounts only through the organization invite link.
                    "LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK": "1",
                    "COLLECT_ANALYTICS": "0",
                    "JSON_LOG": "1",
                },
                "extraEnvironmentSecrets": {
                    "LABEL_STUDIO_USERNAME": {
                        "secretName": ADMIN_SECRET_NAME,
                        "secretKey": "username",  # pragma: allowlist secret
                    },
                    "LABEL_STUDIO_PASSWORD": {
                        "secretName": ADMIN_SECRET_NAME,
                        "secretKey": "password",  # pragma: allowlist secret
                    },
                },
            },
            # The bundled Bitnami Postgres is replaced by RDS above.
            "postgresql": {"enabled": False},
            "app": {
                "podLabels": k8s_app_labels.workload_labels(
                    alert_tier=AlertTier.notify, component=Component.webapp
                ),
                "extraEnvironmentSecrets": {
                    "POSTGRE_USER": {
                        "secretName": DB_CREDS_SECRET_NAME,
                        "secretKey": "DB_USER",  # pragma: allowlist secret
                    },
                    "POSTGRE_PASSWORD": {
                        "secretName": DB_CREDS_SECRET_NAME,
                        "secretKey": "DB_PASS",  # pragma: allowlist secret
                    },
                },
                # The chart ships no requests, which leaves the pod BestEffort.
                # These are a starting point, not a measurement: a handful of
                # annotators on text tasks.
                "resources": {
                    "requests": {"cpu": "250m", "memory": "1Gi"},
                    "limits": {"memory": "2Gi"},
                },
                "nginx": {
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"memory": "256Mi"},
                    },
                },
                "serviceAccount": {
                    "create": True,
                    "name": SERVICE_ACCOUNT_NAME,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": (
                            label_studio_auth_binding.irsa_role.arn
                        ),
                    },
                },
                # Exposed through the ApisixRoute below.
                "ingress": {"enabled": False},
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            label_studio_db,
            label_studio_db_creds,
            label_studio_admin_secret,
            label_studio_auth_binding,
            label_studio_bucket,
        ],
    ),
)

################################
# APISIX: TLS + Keycloak OIDC
################################
OLCertManagerCert(
    f"label-studio-{stack_info.env_suffix}-tls",
    cert_config=OLCertManagerCertConfig(
        application_name="label-studio",
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        create_apisixtls_resource=True,
        dest_secret_name="label-studio-tls",  # noqa: S106  # pragma: allowlist secret
        dns_names=[domain],
    ),
    opts=ResourceOptions(depends_on=[label_studio_release]),
)

label_studio_shared_plugins = OLApisixSharedPlugins(
    name=f"label-studio-shared-plugins-{stack_info.env_suffix}",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="label-studio",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        enable_defaults=True,
        # The UI and its API share one host, so nothing calls it cross-origin.
        enable_cors=False,
    ),
)

label_studio_oidc = OLApisixOIDCResources(
    f"label-studio-oidc-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="label-studio",
        k8s_labels=k8s_labels,
        k8s_namespace=NAMESPACE,
        oidc_scope="openid profile email",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        credentials=OLApisixOIDCVaultCredentials(
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/label-studio",
            vaultauth=label_studio_auth_binding.vault_k8s_resources.auth_name,
        ),
    ),
    opts=ResourceOptions(depends_on=[label_studio_auth_binding.vault_k8s_resources]),
)

OLApisixRoute(
    f"label-studio-{stack_info.env_suffix}-apisix-route",
    k8s_namespace=NAMESPACE,
    k8s_labels=k8s_labels,
    route_configs=[
        # Health check for uptime probes, outside the OIDC gate. It returns a
        # fixed status body and nothing else. Django registers "health/" while
        # the chart's probes call "/health", so both spellings stay ungated.
        OLApisixRouteConfig(
            route_name="label-studio-health",
            priority=10,
            hosts=[domain],
            paths=["/health", "/health/"],
            backend_service_name=APP_NAME,
            backend_service_port=APP_SERVICE_PORT,
            shared_plugin_config_name=label_studio_shared_plugins.resource_name,
        ),
        # Everything else, the browser API calls included, rides the APISIX
        # session. Label Studio's API tokens are not usable through the gateway,
        # since the OIDC plugin redirects any request without a session.
        OLApisixRouteConfig(
            route_name="label-studio-ui",
            priority=0,
            hosts=[domain],
            paths=["/*"],
            backend_service_name=APP_NAME,
            backend_service_port=APP_SERVICE_PORT,
            shared_plugin_config_name=label_studio_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(
                    **label_studio_oidc.get_full_oidc_plugin_config(
                        unauth_action="auth"
                    )
                )
            ],
        ),
    ],
    opts=ResourceOptions(
        depends_on=[
            label_studio_release,
            label_studio_oidc,
            label_studio_shared_plugins,
        ]
    ),
)

export("label_studio_namespace", NAMESPACE)
export("label_studio_url", f"https://{domain}")
export("label_studio_bucket", label_studio_bucket.bucket_v2.bucket)
