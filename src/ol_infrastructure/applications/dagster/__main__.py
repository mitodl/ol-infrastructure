"""Pulumi project for deploying Dagster to Kubernetes using Helm.

This deployment uses:
- The official Dagster Helm chart for the control plane (webserver, daemon)
- Individual Dagster code location images for user code deployments
- The data EKS cluster
- Existing RDS PostgreSQL instance for Dagster's metadata storage
- S3 for compute logs and I/O manager storage
- Vault for secrets management
- APISix ingress with OpenID Connect for authentication
"""

import hashlib
import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    ResourceOptions,
    export,
)
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import (
    DAGSTER_CHART_VERSION,
    PGBOUNCER_EXPORTER_VERSION,
    PGBOUNCER_VERSION,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    ecr_image_uri,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.aws.rds_helper import postgres_max_connections
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

# Config
dagster_config = Config("dagster")
vault_config = Config("vault")


# Stack references
dns_stack = make_stack_reference(projects.DNS, "default")
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
policy_stack = make_stack_reference(projects.POLICIES, "default")
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)
cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
# Owns the Sentry project and client key whose DSN this stack writes to Vault.
# Single-stack (default) because there is one Sentry org, and one Dagster
# project within it shared by every environment.
sentry_stack = make_stack_reference(projects.SENTRY, "default")
# Keycloak is deployed to CI/QA/Production only; the Dev Dagster stack has no
# counterpart to reference, so its data_loading deployment simply goes without
# the Keycloak host and that source stays unavailable there.
keycloak_stack = (
    make_stack_reference(projects.KEYCLOAK_APP, stack_info.name)
    if stack_info.env_suffix in ("ci", "qa", "production")
    else None
)

# VPC and network configuration
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]

# Setup Kubernetes provider
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Environment and tags
dagster_environment = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "data", "Environment": dagster_environment},
)

# Kubernetes labels
k8s_global_labels = K8sGlobalLabels(
    application=Application.dagster,
    product=Product.data,
    service=Services.dagster,
    source_repository="https://github.com/dagster-io/dagster",
    ou=BusinessUnit.data,
    stack=stack_info,
)

aws_account = get_caller_identity()
dagster_namespace = "dagster"

# Verify namespace exists in the cluster
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(dagster_namespace, ns)
)

dagster_bucket_name = f"dagster-{dagster_environment}"
s3_tracking_logs_buckets = [
    f"{edxapp_deployment}-{stack_info.env_suffix}-edxapp-tracking"
    for edxapp_deployment in ("mitxonline", "mitx", "mitx-staging", "xpro")
]
mitlearn_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]
mitlearn_app_buckets = [f"ol-mitlearn-app-storage-{mitlearn_env_suffix}"]
b2b_export_buckets = [f"ol-b2b-partners-storage-{stack_info.env_suffix}"]
dagster_pipeline_buckets = (
    s3_tracking_logs_buckets + mitlearn_app_buckets + b2b_export_buckets
)
dagster_s3_permissions: list[dict[str, str | list[str]]] = [
    {
        "Effect": "Allow",
        "Action": "s3:ListAllMyBuckets",
        "Resource": "*",
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
        ],
        "Resource": [
            f"arn:aws:s3:::{dagster_bucket_name}",
            f"arn:aws:s3:::{dagster_bucket_name}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
        ],
        "Resource": ["arn:aws:s3:::mitx-etl*", "arn:aws:s3:::mitx-etl*/*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
            # Lets Dagster (and operational scripts running as this role) restore
            # objects that have aged into S3 Intelligent-Tiering's archive access
            # tiers, since GetObject on an archived object raises InvalidObjectState
            # until it is explicitly restored.
            "s3:RestoreObject",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
            "s3:DeleteObject",
        ],
        "Resource": [
            f"arn:aws:s3:::*-{stack_info.env_suffix}-edxapp-courses",
            f"arn:aws:s3:::*-{stack_info.env_suffix}-edxapp-courses/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject*",
            "s3:ListBucket",
            "s3:PutObject",
        ],
        "Resource": [
            f"arn:aws:s3:::{bucket_name}" for bucket_name in dagster_pipeline_buckets
        ]
        + [f"arn:aws:s3:::{bucket_name}/*" for bucket_name in dagster_pipeline_buckets],
    },
]

athena_permissions: list[dict[str, str | list[str]]] = [
    {
        "Effect": "Allow",
        "Action": [
            "glue:TagResource",
            "glue:UnTagResource",
        ],
        "Resource": ["*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "glue:BatchCreatePartition",
            "glue:BatchDeletePartition",
            "glue:BatchDeleteTable",
            "glue:BatchGetPartition",
            "glue:CreateDatabase",
            "glue:CreateTable",
            "glue:CreatePartition",
            "glue:DeletePartition",
            "glue:DeleteTable",
            "glue:GetDatabase",
            "glue:GetDatabases",
            "glue:GetPartition",
            "glue:GetPartitions",
            "glue:GetTable",
            "glue:GetTables",
            "glue:UpdateDatabase",
            "glue:UpdatePartition",
            "glue:UpdateTable",
        ],
        "Resource": [
            "arn:aws:glue:*:*:catalog",
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
        ],
    },
]


edxorg_program_credentials_role_assumption = {
    "Effect": "Allow",
    "Action": ["sts:AssumeRole"],
    "Resource": "arn:aws:iam::708756755355:role/mit-s3-edx-program-reports-access",
}

# Combine all IAM permissions for Kubernetes IRSA role
dagster_iam_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        *dagster_s3_permissions,
        *athena_permissions,
        edxorg_program_credentials_role_assumption,
    ],
}

parliament_config = {
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []},
    "CREDENTIALS_EXPOSURE": {"ignore_locations": [{"actions": "sts:assumeRole"}]},
}

# Keep existing S3 buckets (they already exist and store important data)
# Migrated to OLBucket component for standardized management
dagster_runtime_bucket_config = S3BucketConfig(
    bucket_name=dagster_bucket_name,
    versioning_enabled=True,
    server_side_encryption_enabled=True,
    tags=aws_config.tags,
)
dagster_runtime_bucket = OLBucket(
    "dagster-runtime",
    config=dagster_runtime_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"dagster-{dagster_environment}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="dagster-runtime-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="dagster-runtime-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="dagster-runtime-bucket-server-side-encryption",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Bucket to store gcs import of edxorg course tarballs
# Migrated to OLBucket component for standardized management
edxorg_courses_bucket_name = f"edxorg-{stack_info.env_suffix}-edxapp-courses"
edxorg_courses_bucket_config = S3BucketConfig(
    bucket_name=edxorg_courses_bucket_name,
    versioning_enabled=True,
    server_side_encryption_enabled=True,
    # edX.org course tarballs are cold archive data: safe for archive tiers.
    intelligent_tiering_archive_access_days=90,
    intelligent_tiering_deep_archive_access_days=180,
    tags=aws_config.tags,
)
edxorg_courses_bucket = OLBucket(
    "edxorg-courses",
    config=edxorg_courses_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=edxorg_courses_bucket_name,
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxorg-courses-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxorg-courses-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="edxorg-courses-bucket-server-side-encryption",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)


# Security group for RDS database - updated to allow Kubernetes pod access
dagster_db_security_group = ec2.SecurityGroup(
    f"dagster-db-access-{stack_info.env_suffix}",
    name=f"ol-etl-db-access-{stack_info.env_suffix}",
    description="Access from the data VPC to the Dagster database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access from Vault for database backend",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs,
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access from Kubernetes pods in data cluster",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=data_vpc["id"],
)

# Keep existing RDS database (Dagster metadata storage)
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["monitoring_profile_name"] = "disabled"
rds_defaults["enhanced_monitoring_interval"] = 0
rds_defaults["performance_insights_enabled"] = False
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
rds_defaults["instance_size"] = (
    dagster_config.get("db_instance_type") or rds_defaults["instance_size"]
)
dagster_db_config = OLPostgresDBConfig(
    db_name="dagster",
    instance_name=f"ol-etl-db-{stack_info.env_suffix}",
    max_storage=1000,
    password=get_config("dagster:db_password"),
    security_groups=[dagster_db_security_group],
    subnet_group_name=data_vpc["rds_subnet"],
    tags=aws_config.tags,
    **rds_defaults,
)
dagster_db = OLAmazonDB(dagster_db_config)

# Keep existing Vault database backend
dagster_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=dagster_db_config.db_name,
    mount_point=f"{dagster_db_config.engine}-dagster",
    db_admin_username=dagster_db_config.username,
    db_admin_password=get_config("dagster:db_password"),
    db_host=dagster_db.db_instance.address,
)
dagster_db_vault_backend = OLVaultDatabaseBackend(dagster_db_vault_backend_config)


# ============================================================================
# Kubernetes Deployment using Helm
# ============================================================================

# OLEKSAuthBinding for IRSA and Vault K8s auth
dagster_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="dagster",
        namespace=dagster_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=dagster_iam_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("dagster_server_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=["dagster", "dagster-user-code"],
        vault_sync_service_account_names=[
            "dagster",
            "dagster-vault",
            "dagster-user-code",
        ],
        k8s_labels=k8s_global_labels,
        parliament_config=parliament_config,
        # Dagster pipelines can run for several hours (e.g. bulk edxorg S3 loads).
        # The AWS default of 1 hour causes ExpiredToken errors mid-pipeline.
        # 12 hours (the AWS maximum) gives ample headroom for all workloads.
        irsa_max_session_duration=43200,
    )
)

dagster_vault_iam_role = vault.aws.SecretBackendRole(
    f"ol-mitopen-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name="dagster",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "data", "vault_managed": "True"},
    policy_arns=[dagster_auth_binding.iam_policy.arn],
)


# Create Vault secrets for Dagster configuration
dagster_static_secrets = OLVaultK8SSecret(
    f"dagster-k8s-static-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-static-secrets",  # pragma: allowlist secret  # noqa: E501, S106
        exclude_raw=True,
        excludes=[".*"],
        labels=k8s_global_labels.model_dump(),
        mount="secret-data",
        mount_type="kv-v1",
        name="dagster-static-secrets",
        namespace=dagster_namespace,
        path="dagster-http-auth-password",
        refresh_after="1m",
        templates={
            "DAGSTER_AIRBYTE_AUTH": '{{ printf "dagster:%s" (get .Secrets "dagster_unhashed_password") }}',  # pragma: allowlist secret  # noqa: E501
        },
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

dagster_dbt_secrets = OLVaultK8SSecret(
    f"dagster-k8s-dbt-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-dbt-secrets",  # pragma: allowlist secret  # noqa: E501, S106
        exclude_raw=True,
        excludes=[".*"],
        labels=k8s_global_labels.model_dump(),
        mount="secret-data",
        mount_type="kv-v1",
        name="dagster-dbt-secrets",
        namespace=dagster_namespace,
        path="dagster-dbt-creds",
        refresh_after="1m",
        templates={
            "DBT_TRINO_USERNAME": '{{ get .Secrets "username" }}',
            "DBT_TRINO_PASSWORD": '{{ get .Secrets "password" }}',
        },
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

# The DSN is created by the ol-infrastructure-sentry stack, which owns the
# Sentry project and its client key, so Pulumi manages the value end to end and
# nobody has to hand-write it into Vault. secret-data is a pre-existing kv-v1
# mount shared by the other Dagster secrets, so only the secret itself is
# declared here, not the mount.
dagster_sentry_vault_secret = vault.generic.Secret(
    f"dagster-sentry-dsn-{stack_info.env_suffix}",
    path="secret-data/dagster/sentry",
    data_json=sentry_stack.require_output("dagster_sentry_dsn").apply(
        lambda dsn: json.dumps({"dsn": dsn})
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# Sentry DSN for the code locations and run workers. Kept as its own secret
# rather than folded into dagster-static-secrets because an OLVaultK8SSecret
# reads a single Vault path, and the DSN is owned by the sentry stack.
dagster_sentry_secrets = OLVaultK8SSecret(
    f"dagster-k8s-sentry-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-sentry-secrets",  # pragma: allowlist secret  # noqa: E501, S106
        exclude_raw=True,
        excludes=[".*"],
        labels=k8s_global_labels.model_dump(),
        mount="secret-data",
        mount_type="kv-v1",
        name="dagster-sentry-secrets",
        namespace=dagster_namespace,
        path="dagster/sentry",
        refresh_after="1m",
        templates={
            "SENTRY_DSN": '{{ get .Secrets "dsn" }}',
        },
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        depends_on=[dagster_auth_binding, dagster_sentry_vault_secret]
    ),
)

# Create Vault dynamic secret for database credentials
dagster_db_secret = OLVaultK8SSecret(
    f"dagster-k8s-db-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SDynamicSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-postgresql-secret",  # pragma: allowlist secret  # noqa: E501, S106
        labels=k8s_global_labels.model_dump(),
        mount="postgres-dagster",
        name="dagster-postgresql-secret",
        namespace=dagster_namespace,
        path="creds/app",
        refresh_after="1h",
        # Restart PgBouncer when credentials rotate so the init container
        # re-renders pgbouncer.ini with fresh credentials.
        restart_target_kind="Deployment",
        restart_target_name="dagster-pgbouncer",
        revoke_on_delete=True,
        role_name="app",
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
        # Map Vault's fields to both Dagster Helm chart format and environment variables
        templates={
            "postgresql-password": "{{ .Secrets.password }}",
            "DAGSTER_PG_PASSWORD": "{{ .Secrets.password }}",
            "DAGSTER_PG_USER": "{{ .Secrets.username }}",
        },
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

# ============================================================================
# Standalone PgBouncer for RDS connection pooling
# ============================================================================
# Uses ghcr.io/cloudnative-pg/pgbouncer which reads /etc/pgbouncer/pgbouncer.ini.
# An init container renders the config template by substituting credentials from the
# Vault-managed dagster-postgresql-secret before PgBouncer starts.
#
# auth_type=any: PgBouncer accepts all inbound connections without client auth.
# This is safe because the Service is ClusterIP (namespace-internal only) and
# real scram-sha-256 auth is enforced on the backend connection to RDS via the
# user/password embedded in the [databases] DSN.

# Replica count is needed here to size the per-pod connection cap below, so it is
# resolved before the ConfigMap rather than next to the Deployment that consumes it.
pgbouncer_replica_count = dagster_config.get_int("pgbouncer_replica_count") or 2

# Cap the connections PgBouncer can open against RDS, in aggregate across every replica.
#
# Without this, nothing bounds the total. In session pool mode each client pins a server
# connection for its whole session, so the per-pod bound is
# min(max_client_conn, default_pool_size + reserve_pool_size) = 1500 -- which at 6
# replicas is 9000 possible backends against a hard max_connections of 5000. On
# 2026-08-10 the pool reached that limit and held it for 88 consecutive minutes
# (DatabaseConnections pinned at 4989 = 5000 - 5 reserved - 6 rdsadmin sessions), during
# which every new Dagster connection was refused and the daemon sat Pending behind 178
# run workers. PgBouncer was configured such that it could exhaust the database it
# exists to protect.
#
# Deriving from the instance class rather than hardcoding 5000 matters because
# max_connections tracks instance memory -- QA's db.m7g.large allows ~900, so a
# hardcoded production number would leave QA overcommitted by the same 1.8x.
# The headroom leaves room for superuser_reserved_connections, reserved_connections,
# RDS's own rdsadmin sessions, Vault credential rotation logins, ad-hoc psql, any
# future direct-to-RDS consumer such as a metrics exporter, and -- on instance classes
# below the 5000 cap -- the difference between total instance memory and the smaller
# DBInstanceClassMemory that RDS actually divides (see postgres_max_connections).
#
# That last term is the reason 0.85 is not as generous as it looks on the QA stack.
# postgres_max_connections computes 901 there but the instance really allows 832, so
# the resulting 764 aggregate leaves 68 connections rather than the ~135 the factor
# implies -- still comfortably clear of the ~11 reserved and administrative
# connections, but worth knowing before anyone raises this factor. Production is
# unaffected: the 5000 cap binds, so its figure is exact.
#
# Dividing by pgbouncer_replica_count makes the aggregate ceiling depend on the running
# pod count, which is why the Deployment below pins max_surge to 0.
DB_CONNECTION_HEADROOM_FACTOR = 0.85
pgbouncer_max_db_connections = int(
    postgres_max_connections(rds_defaults["instance_size"])
    * DB_CONNECTION_HEADROOM_FACTOR
    // pgbouncer_replica_count
)

# ConfigMap containing the pgbouncer.ini template; ${PGUSER} and ${PGPASSWORD}
# placeholders are substituted at pod start time by the init container.
pgbouncer_config = kubernetes.core.v1.ConfigMap(
    f"dagster-pgbouncer-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pgbouncer-config",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={
        "pgbouncer.ini.template": dagster_db.db_instance.address.apply(
            lambda addr: "\n".join(
                [
                    "[databases]",
                    f"dagster = host={addr} port={DEFAULT_POSTGRES_PORT}"
                    " dbname=dagster user=${PGUSER} password=${PGPASSWORD}",
                    "",
                    "[pgbouncer]",
                    "listen_addr = 0.0.0.0",
                    "listen_port = 5432",
                    "auth_type = any",
                    "pool_mode = session",
                    "max_client_conn = 1500",
                    "default_pool_size = 800",
                    "min_pool_size = 150",
                    "reserve_pool_size = 2000",
                    # The aggregate ceiling. See the derivation above; this is the
                    # only setting here that bounds total backends across replicas,
                    # and it converts "exhaust RDS" into "queue inside PgBouncer",
                    # which is the failure mode query_wait_timeout = 0 below was
                    # already chosen to tolerate.
                    f"max_db_connections = {pgbouncer_max_db_connections}",
                    "max_prepared_statements = 0",
                    "server_connect_timeout = 15",
                    # Dagster uses NullPool with AUTOCOMMIT isolation - no session
                    # state is left between queries, so DISCARD ALL is unnecessary
                    # and adds latency + a race-condition window on each disconnect.
                    "server_reset_query =",
                    # PgBouncer 1.25 defaults server_check_query to empty (disabled).
                    # Re-enable it so PgBouncer verifies server connections are alive
                    # before assigning them to clients. server_check_delay=30 means
                    # any connection idle >30s is health-checked before use. Note:
                    # server_check_delay=0 DISABLES checks (PgBouncer behavior since
                    # v1.7 when the default was changed from 0 to 30).
                    "server_check_query = ;",
                    "server_check_delay = 30",
                    # Proactively recycle backend connections every 30 min to prevent
                    # stale connections accumulating.
                    "server_lifetime = 1800",
                    # Release idle backend connections after 2 min. This is
                    # intentionally shorter than RDS's tcp_keepalives_idle (300s) so
                    # PgBouncer always closes idle connections before RDS silently
                    # terminates them, preventing zombie connections from being
                    # assigned to clients.
                    "server_idle_timeout = 120",
                    # Raised well above the 120s default, but NOT disabled.
                    #
                    # The default was too short: during heavy RDS checkpoint I/O,
                    # queries slow from milliseconds to seconds and the pool backs up,
                    # and at 120s clients that can't immediately get a server
                    # connection are disconnected with "server closed the connection
                    # unexpectedly" in psycopg2. That pressure clears within 1-2
                    # minutes, so the fix was to wait it out.
                    #
                    # This was previously 0, which disables the timeout entirely, and
                    # that turned out to be a different failure rather than the absence
                    # of one. A timeout is the only thing that breaks a pool deadlock:
                    # when every server connection is held by a client that is itself
                    # blocked waiting for a server connection, nothing is released
                    # until something gives up. With 0, nothing ever gives up. QA sat
                    # in exactly that state for days on 2026-08-17 -- all 764 backends
                    # pinned, sv_idle 0, the oldest client queued 2.4 days, the daemon
                    # frozen mid-backfill-cancellation -- and it could only be cleared
                    # by deleting run workers by hand.
                    #
                    # 600s keeps the original intent with a large margin (5x the 1-2
                    # minutes checkpoint pressure actually lasts) while restoring the
                    # property that a wedged pool eventually unwedges itself. A client
                    # that waits ten minutes for a connection is not going to be
                    # rescued by waiting longer; it needs to fail so the pool can drain
                    # and Dagster's own retry logic can take over.
                    "query_wait_timeout = 600",
                    # Dagster uses NullPool, so it opens a fresh connection per query
                    # and every connection logs with age=0s. Measured on one production
                    # pod: 27,738 lines in 3 minutes, ~154 lines/s per replica and
                    # ~925/s across six, all of it shipped to Loki. The one line that
                    # carries signal -- the per-minute `stats:` aggregate -- was 1 in
                    # ~9,000. The pgbouncer_exporter sidecar now collects the same
                    # connection counts as metrics, so these logs are pure cost.
                    "log_connections = 0",
                    "log_disconnections = 0",
                    # Required by pgbouncer_exporter: its PostgreSQL driver sends
                    # extra_float_digits on connect, and PgBouncer rejects unknown
                    # startup parameters unless they are listed here.
                    "ignore_startup_parameters = extra_float_digits",
                    "application_name_add_host = 1",
                    "",
                ]
            )
        )
    },
    # The provider replaces this ConfigMap on any data change, and its name is fixed,
    # so the replacement can only be delete-then-create. Make that ordering explicit
    # rather than letting the default create-before-delete attempt fail on
    # "configmaps ... already exists" and recover on the retry.
    opts=ResourceOptions(depends_on=[dagster_db_secret], delete_before_replace=True),
)

# PgBouncer Deployment; replica count (default 2, for HA) is resolved above.
pgbouncer_deployment = kubernetes.apps.v1.Deployment(
    f"dagster-pgbouncer-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pgbouncer",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
        annotations={
            "pulumi.com/patchForce": "true",
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=pgbouncer_replica_count,
        # max_db_connections above is sized as budget / pgbouncer_replica_count, so the
        # aggregate ceiling only holds while the running pod count stays at or below the
        # desired replica count. Kubernetes' default 25% maxSurge would allow 8 pods in
        # production and 3 in QA during a rollout -- 5664 and 1146 backends, both past
        # the database limit the cap exists to respect, and reached precisely while
        # rolling out a change to this Deployment. Surge to zero and replace in place.
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_surge=0,
                max_unavailable=1,
            ),
        ),
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={
                "component": "pgbouncer",
                **k8s_global_labels.model_dump(),
            },
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={
                    "component": "pgbouncer",
                    **k8s_global_labels.model_dump(),
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                init_containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="render-config",
                        image=f"ghcr.io/cloudnative-pg/pgbouncer:{PGBOUNCER_VERSION}",
                        command=[
                            "/bin/sh",
                            "-c",
                            "sed"
                            ' -e "s/\\${PGUSER}/$PGUSER/g"'
                            ' -e "s/\\${PGPASSWORD}/$PGPASSWORD/g"'
                            " /config-template/pgbouncer.ini.template"
                            " > /config-out/pgbouncer.ini",
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGUSER",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="dagster-postgresql-secret",
                                        key="DAGSTER_PG_USER",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGPASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="dagster-postgresql-secret",
                                        key="DAGSTER_PG_PASSWORD",
                                    ),
                                ),
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="config-template",
                                mount_path="/config-template",
                                read_only=True,
                            ),
                            kubernetes.core.v1.VolumeMountArgs(
                                name="config-out",
                                mount_path="/config-out",
                            ),
                        ],
                    ),
                ],
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="pgbouncer",
                        image=f"ghcr.io/cloudnative-pg/pgbouncer:{PGBOUNCER_VERSION}",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="pgbouncer",
                                container_port=5432,
                                protocol="TCP",
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="config-out",
                                mount_path="/etc/pgbouncer",
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "100m",
                                "memory": "128Mi",
                            },
                            limits={
                                "memory": "256Mi",
                            },
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=5432,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=10,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=5432,
                            ),
                            initial_delay_seconds=5,
                            period_seconds=5,
                        ),
                    ),
                    # Sidecar exporting PgBouncer's admin console as Prometheus
                    # metrics. It polls SHOW LISTS/STATS/POOLS/DATABASES over
                    # localhost and serves /metrics on 9127.
                    #
                    # No password and no PgBouncer auth config are needed: under
                    # auth_type = any the console database admits any user as admin.
                    # Verified against a production pod -- this exact DSN returns
                    # SHOW POOLS. Reaching the console still requires being inside
                    # the pod's network namespace, and the Service is ClusterIP.
                    kubernetes.core.v1.ContainerArgs(
                        name="pgbouncer-exporter",
                        image=(
                            "quay.io/prometheuscommunity/pgbouncer-exporter:"
                            f"{PGBOUNCER_EXPORTER_VERSION}"
                        ),
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGBOUNCER_EXPORTER_CONNECTION_STRING",
                                value=(
                                    "postgres://exporter@127.0.0.1:5432"
                                    "/pgbouncer?sslmode=disable"
                                ),
                            ),
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="metrics",
                                container_port=9127,
                                protocol="TCP",
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "10m",
                                "memory": "32Mi",
                            },
                            limits={
                                "memory": "64Mi",
                            },
                        ),
                        # The exporter is not in the data path -- a failing scrape
                        # must never take PgBouncer's endpoint out of the Service and
                        # sever Dagster's connection to the database. So it gets a
                        # liveness probe to restart itself if it wedges, and
                        # deliberately no readiness probe.
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/metrics",
                                port=9127,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                            failure_threshold=3,
                        ),
                    ),
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="config-template",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name="dagster-pgbouncer-config",
                        ),
                    ),
                    kubernetes.core.v1.VolumeArgs(
                        name="config-out",
                        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[dagster_db_secret, pgbouncer_config]),
)

# PgBouncer Service
pgbouncer_service = kubernetes.core.v1.Service(
    f"dagster-pgbouncer-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pgbouncer",
        namespace=dagster_namespace,
        # component=pgbouncer is on the Service itself, not just the pod selector,
        # so the ServiceMonitor below can select this Service without also matching
        # the other Services in the namespace that share the global labels.
        labels={
            "component": "pgbouncer",
            **k8s_global_labels.model_dump(),
        },
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={
            "component": "pgbouncer",
            **k8s_global_labels.model_dump(),
        },
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="pgbouncer",
                port=5432,
                target_port=5432,
                protocol="TCP",
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="metrics",
                port=9127,
                target_port=9127,
                protocol="TCP",
            ),
        ],
    ),
    opts=ResourceOptions(depends_on=[pgbouncer_deployment]),
)

# ServiceMonitor so Prometheus scrapes the exporter sidecar on every replica.
#
# The cluster's k8s-monitoring collector has prometheusOperatorObjects enabled and
# remote-writes to Grafana Cloud, so no pipeline work is needed -- this CR is the
# whole integration. Pattern (including the "release": "prometheus" discovery label)
# follows applications/clickhouse/__main__.py.
#
# These are the metrics every open pool-sizing question turns on:
#   pgbouncer_pools_server_active_connections   -- is default_pool_size = 800 right?
#   pgbouncer_pools_server_idle_connections     -- what is min_pool_size parking?
#   pgbouncer_pools_client_active_connections   -- is max_client_conn a real ceiling?
#   pgbouncer_pools_client_waiting_connections  -- is max_db_connections too tight?
#   pgbouncer_pools_client_maxwait_seconds      -- the number pool tuning turns on,
#                                                  unobtainable from the stats log
#   sum of the above vs. max_connections        -- the 2026-08-10 headroom alert
pgbouncer_service_monitor = kubernetes.apiextensions.CustomResource(
    f"dagster-pgbouncer-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pgbouncer",
        namespace=dagster_namespace,
        labels={
            **k8s_global_labels.model_dump(),
            # Label required for Prometheus Operator to discover this ServiceMonitor
            "release": "prometheus",
        },
    ),
    spec={
        "selector": {
            "matchLabels": {
                "component": "pgbouncer",
                **k8s_global_labels.model_dump(),
            },
        },
        "namespaceSelector": {"matchNames": [dagster_namespace]},
        "endpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "scheme": "http",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "relabelings": [
                    {
                        "sourceLabels": ["__meta_kubernetes_pod_name"],
                        "targetLabel": "pod",
                    },
                    {
                        "sourceLabels": ["__meta_kubernetes_namespace"],
                        "targetLabel": "namespace",
                    },
                ],
            }
        ],
    },
    opts=ResourceOptions(depends_on=[pgbouncer_service]),
)

# APISix OIDC configuration for authentication
dagster_oidc_resources = OLApisixOIDCResources(
    f"dagster-k8s-apisix-oidc-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="dagster",
        k8s_labels=k8s_global_labels.model_dump(),
        k8s_namespace=dagster_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{dagster_config.require('domain')}/",
        oidc_session_absolute_timeout=60 * 20160,  # 14 days
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        oidc_use_session_secret=True,
        oidc_scope="openid profile email",
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/dagster",
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)


# Create ConfigMap for AWS profile configuration to handle cross-account access This
# allows the edxorg code location to assume a role in the edX.org AWS account For EKS
# with IRSA, the default profile specifies the IRSA role ARN and web identity token
# file. The edxorg profile then uses those credentials to assume the cross-account role.
def create_aws_config(irsa_role_arn: str) -> str:
    return f"""[default]
region = us-east-1
web_identity_token_file = /var/run/secrets/eks.amazonaws.com/serviceaccount/token
role_arn = {irsa_role_arn}

[profile edxorg]
role_arn = arn:aws:iam::708756755355:role/mit-s3-edx-program-reports-access
role_session_name = replicate-program-credentials-reports
source_profile = default
"""


aws_profile_configmap = kubernetes.core.v1.ConfigMap(
    f"dagster-aws-profile-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-aws-profile-config",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={
        "config": dagster_auth_binding.irsa_role.arn.apply(create_aws_config),
    },
)

# Create Vault secret for edxorg GCP credentials used by legacy_openedx pipelines
edxorg_gcp_secret = OLVaultK8SSecret(
    f"dagster-k8s-edxorg-gcp-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-edxorg-gcp-secrets",  # pragma: allowlist secret  # noqa: E501, S106
        exclude_raw=True,
        excludes=[".*"],
        labels=k8s_global_labels.model_dump(),
        mount="secret-data",
        mount_type="kv-v1",
        name="dagster-edxorg-gcp-secrets",
        namespace=dagster_namespace,
        path="pipelines/edx/org/gcp-oauth-client",
        refresh_after="24h",
        templates={
            "edxorg_gcp.yaml": """resources:
  gcp_gcs:
    config:
      auth_uri: {{ get .Secrets "url" }}
      client_email: {{ get .Secrets "client_email" }}
      client_id: "{{ get .Secrets "client_id" }}"
      client_x509_cert_url: {{ get .Secrets "cert_url" }}
      private_key: |
{{ get .Secrets "private_key" | indent 8 }}
      private_key_id: {{ get .Secrets "private_key_id" }}
      project_id: {{ get .Secrets "project_id" }}
      token_uri: {{ get .Secrets "token_uri" }}""",
        },
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

# Define the user code deployments before the main helm chart so they can be referenced
# Define all code locations based on ol-data-platform structure
code_locations: list[dict[str, str | int]] = [
    {"name": "canvas", "module": "canvas.definitions", "port": 4000},
    {"name": "data_loading", "module": "data_loading.definitions", "port": 4000},
    {"name": "data_platform", "module": "data_platform.definitions", "port": 4001},
    {"name": "edxorg", "module": "edxorg.definitions", "port": 4002},
    {"name": "lakehouse", "module": "lakehouse.definitions", "port": 4003},
    {
        "name": "learning_resources",
        "module": "learning_resources.definitions",
        "port": 4004,
    },
    {"name": "legacy_openedx", "module": "legacy_openedx.definitions", "port": 4005},
    {"name": "openedx", "module": "openedx.definitions", "port": 4006},
    {
        "name": "b2b_organization",
        "module": "b2b_organization.definitions",
        "port": 4007,
    },
    {
        "name": "student_risk_probability",
        "module": "student_risk_probability.definitions",
        "port": 4008,
    },
]

# Build deployments list for user code
# Code locations that run 2 replicas for resilience. Criteria: OOM-prone,
# slow-starting (making restarts expensive), or high sensor-tick frequency.
multi_replica_locations = {"legacy_openedx", "lakehouse", "data_loading"}

deployments = []
for location in code_locations:
    name: str = location["name"]  # type: ignore[assignment]
    module: str = location["module"]  # type: ignore[assignment]
    port: int = location["port"]  # type: ignore[assignment]

    # Get image tag from environment variable set by Concourse pipeline
    # The pipeline tags each image with the git short-ref of the commit
    env_var_name = f"DAGSTER_{name.upper()}_IMAGE_TAG"
    image_tag = os.environ.get(env_var_name)

    # Use the tag from the environment variable if available, otherwise fallback to
    # config or latest
    if image_tag:
        image_tag_or_digest = image_tag
    else:
        # Fallback to tag-based reference
        image_tag_or_digest = dagster_config.get("docker_image_tag") or "latest"

    deployment = {
        "name": name.replace("_", "-"),
        "image": {
            "repository": ecr_image_uri(f"mitodl/dagster-{name}"),
            "tag": image_tag_or_digest,
            "pullPolicy": "IfNotPresent",
        },
        # Chart key is deploymentStrategy, not strategy.
        "deploymentStrategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
        },
        "dagsterApiGrpcArgs": [
            "-m",
            module,
        ],
        "port": port,
        "startupProbe": {
            "enabled": True,
            "periodSeconds": 10,
            "timeoutSeconds": 10,
            "failureThreshold": 60,
            # Allow time for container and gRPC server initialization
            "initialDelaySeconds": 15,
        },
        "readinessProbe": {
            "enabled": True,
            "periodSeconds": 20,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
            "initialDelaySeconds": 10,
        },
        "livenessProbe": {
            "enabled": True,
            "periodSeconds": 30,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
            "initialDelaySeconds": 60,
        },
        "annotations": dagster_auth_binding.irsa_role.arn.apply(
            lambda arn: {
                "eks.amazonaws.com/role-arn": arn,
            }
        ),
        "resources": {
            "requests": {
                "cpu": "500m",
                "memory": "1Gi",
            },
            "limits": {
                "cpu": "3000m",
                "memory": "8Gi",
            },
        },
        "env": [
            {
                "name": "DAGSTER_SENSOR_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_MAX_SEND_BYTES",
                "value": "536870912",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "536870912",
            },
            {
                "name": "DAGSTER_PG_HOST",
                "value": "dagster-pgbouncer.dagster.svc.cluster.local",
            },
            {"name": "DAGSTER_PG_DB", "value": "dagster"},
            {"name": "DAGSTER_BUCKET_NAME", "value": dagster_bucket_name},
            {"name": "DAGSTER_ENVIRONMENT", "value": stack_info.env_suffix},
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
            {"name": "DAGSTER_VAULT_ROLE", "value": "dagster"},
            # Ties each Sentry issue to the image it came from. Same git
            # short-ref the Concourse pipeline tagged the image with.
            {"name": "SENTRY_RELEASE", "value": image_tag_or_digest},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
            {"name": "dagster-postgresql-secret"},
            {"name": "dagster-sentry-secrets"},
        ],
    }

    # data_loading's dlt database sources connect by host and take their
    # credentials from Vault at run time, so only the host is passed through
    # here -- from the owning application's stack, not duplicated in config.
    if name == "data_loading" and keycloak_stack is not None:
        deployment["env"].append(
            {
                "name": "KEYCLOAK_DB_HOST",
                "value": keycloak_stack.require_output("keycloak")["rds_host"],
            }
        )

    # Add higher resources for lakehouse deployment (runs dbt)
    if name == "lakehouse":
        deployment["resources"] = {
            "requests": {
                "cpu": "1000m",
                "memory": "2Gi",
            },
            "limits": {
                "cpu": "4000m",
                "memory": "8Gi",
            },
        }
        # Lakehouse has slow definitions loading (~19s) due to Airbyte+dbt integration.
        # Increase startup probe delays to prevent race condition when webserver tries
        # to connect before the code location has finished initializing.
        deployment["startupProbe"] = {
            "enabled": True,
            "periodSeconds": 10,
            # Allow longer timeout for health check response
            "timeoutSeconds": 10,
            # 1200s total (10s * 120), gives 20s for definitions to load
            "failureThreshold": 120,
            # Delay before first check to let container fully initialize
            "initialDelaySeconds": 30,
        }
        deployment["readinessProbe"] = {
            "enabled": True,
            # Less aggressive after startup
            "periodSeconds": 20,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
            "initialDelaySeconds": 30,
        }
        # dbt artifact upload for OpenMetadata ingestion
        deployment["env"].extend(
            [
                {
                    "name": "DBT_ARTIFACTS_S3_BUCKET",
                    "value": dagster_bucket_name,
                },
                {
                    "name": "DBT_ARTIFACTS_S3_PREFIX",
                    "value": "openmetadata/dbt-artifacts",
                },
            ]
        )

    # Increase RAM for legacy edX because of studentmodule loading to memory
    if name == "legacy_openedx":
        deployment["resources"] = {
            "requests": {
                "cpu": "1000m",
                "memory": "4Gi",
            },
            "limits": {
                "cpu": "4000m",
                "memory": "32Gi",
            },
        }

    # Add AWS profile configuration for edxorg deployment to handle cross-account access
    if name == "edxorg":
        deployment["volumes"] = [
            {
                "name": "aws-config",
                "configMap": {"name": "dagster-aws-profile-config"},
            }
        ]
        deployment["volumeMounts"] = [
            {
                "name": "aws-config",
                "mountPath": "/etc/aws",
                "readOnly": True,
            }
        ]
        # Set AWS_CONFIG_FILE to use the mounted configuration
        deployment["env"].append(
            {"name": "AWS_CONFIG_FILE", "value": "/etc/aws/config"}
        )
        # Give more memory for processing edxorg archives, which are large and
        # cause OOM kills at lower limits.
        deployment["resources"] = {
            "requests": {
                "cpu": "500m",
                "memory": "1Gi",
            },
            "limits": {
                "memory": "32Gi",
            },
        }

    if name in multi_replica_locations:
        deployment["replicaCount"] = 2
        # Spread replicas across nodes so a single node failure doesn't take
        # both down. Soft preference (preferred) avoids scheduling failures on
        # small clusters.
        deployment["affinity"] = {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {
                                "matchLabels": {
                                    "deployment": name.replace("_", "-"),
                                }
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        }

    deployments.append(deployment)

# Referenced by run_launcher.config.run_k8s_config.pod_spec_config in
# dagster_instance.yaml -- see the rationale there. A pod naming a
# nonexistent PriorityClass is rejected outright, so this has to exist before
# the instance ConfigMap that points at it (hence the Helm release's
# depends_on below).
#
# The value is negative deliberately: run workers rank below every priority-0
# pod in the cluster, not just the ones in this namespace. That is broader
# than the incident strictly requires, but these pods have already starved
# StarRocks FE out of the core nodegroup for 5 days (#5183), so ranking batch
# work below anything long-lived is the designation we want. preemption_policy
# Never keeps the relationship one-directional -- run workers queue for
# capacity, they never evict anything themselves.
dagster_run_priority_class = kubernetes.scheduling.v1.PriorityClass(
    f"dagster-run-priority-class-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-run",
        labels=k8s_global_labels.model_dump(),
    ),
    value=-100,
    preemption_policy="Never",
    global_default=False,
    description=(
        "Dagster run workers: preemptible batch work that ranks below all "
        "control-plane pods."
    ),
)

# How many runs the QueuedRunCoordinator will have in flight at once.
#
# This has to be sized against the environment's PgBouncer connection budget,
# not set globally. Dagster's storage uses NullPool, so a run worker opens a
# fresh connection per query and holds one per concurrent step; measured on
# data-qa on 2026-08-17, 32 run workers were holding ~700 client connections
# between them, around 22 each. At the previous global value of 100, that is
# ~2200 connections -- fine against production's aggregate cap of 4248, and
# 2.9x QA's 764.
#
# QA duly deadlocked. Once the pool was full, every remaining client queued
# behind it, and because query_wait_timeout was 0 (see below) none of them ever
# timed out. The run workers held connections while waiting on connections that
# could only be released by the workers themselves, the daemon blocked
# mid-way through cancelling a backfill, and the whole stack sat frozen for
# days -- 764/764 servers pinned, sv_idle 0, one client queued 2.4 days.
# Nothing could recover on its own because every recovery path needed the
# connection that was unavailable.
#
# Defaulting to 100 keeps production exactly as it was; QA sets a lower value
# in its stack config.
dagster_max_concurrent_runs = dagster_config.get_int("max_concurrent_runs") or 100

# Custom Dagster instance ConfigMap with dynamic credentials support
# Note: We create this before the Helm release so it gets proper ownership
dagster_instance_yaml = (
    Path(__file__)
    .parent.joinpath("dagster_instance.yaml")
    .read_text()
    .replace("MAX_CONCURRENT_RUNS", str(dagster_max_concurrent_runs))
)
# The daemon and webserver mount dagster.yaml from the dagster-instance
# ConfigMap via subPath, which kubelet never live-refreshes, and the chart's
# built-in checksum/dagster-instance rollout trigger hashes only its OWN
# values-rendered instance template -- not the extraManifests override below
# that actually carries this file. Without this annotation, edits to
# dagster_instance.yaml deploy the ConfigMap but silently never reach the
# running processes until someone manually restarts them (bit us on the run
# Job TTL fix, #5373): injecting our own content hash into both pod templates
# makes a config edit roll the pods the same way the chart's native checksum
# does.
dagster_instance_checksum_annotation = {
    "checksum/ol-dagster-instance": hashlib.sha256(
        dagster_instance_yaml.encode()
    ).hexdigest(),
}

# Get dagster-k8s image tag from environment variable (set by Concourse)
dagster_k8s_image_tag = os.environ.get("DAGSTER_K8S_IMAGE_TAG")
if dagster_k8s_image_tag:
    dagster_k8s_image_tag_or_digest = dagster_k8s_image_tag
else:
    # Fallback to tag-based reference
    dagster_k8s_image_tag_or_digest = dagster_config.get("docker_image_tag") or "latest"

dagster_k8s_image_config = {
    "repository": ecr_image_uri("mitodl/dagster-k8s"),
    "tag": dagster_k8s_image_tag_or_digest,
    "pullPolicy": "IfNotPresent",
}

# Dagster Helm chart values
dagster_helm_values = {
    "global": {
        "serviceAccountName": "dagster",
        "postgresqlSecretName": "dagster-postgresql-secret",  # pragma: allowlist secret  # noqa: E501
    },
    "dagster-user-deployments": {"enabled": True, "enableSubchart": False},
    "serviceAccount": {
        "create": True,
        "name": "dagster",
        "annotations": {
            "eks.amazonaws.com/role-arn": dagster_auth_binding.irsa_role.arn,
        },
    },
    # Dagster webserver (UI)
    "dagsterWebserver": {
        "annotations": dagster_instance_checksum_annotation,
        "image": dagster_k8s_image_config,
        "workspace": {
            "enabled": True,
            "servers": [
                {"host": deployment["name"], "port": deployment["port"]}
                for deployment in deployments
            ],
        },
        "replicaCount": 2,
        "service": {
            "type": "ClusterIP",
            "port": 3000,
        },
        "resources": {
            "requests": {
                "cpu": "500m",
                "memory": "1Gi",
            },
            "limits": {
                "cpu": "2000m",
                "memory": "4Gi",
            },
        },
        "livenessProbe": {
            "httpGet": {
                "path": "/server_info",
                "port": 3000,
            },
            "initialDelaySeconds": 60,
            "periodSeconds": 30,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
        },
        "readinessProbe": {
            "httpGet": {
                "path": "/server_info",
                "port": 3000,
            },
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
        },
        "env": [
            {
                "name": "DAGSTER_PG_HOST",
                "value": "dagster-pgbouncer.dagster.svc.cluster.local",
            },
            {"name": "DAGSTER_PG_DB", "value": "dagster"},
            {"name": "DAGSTER_BUCKET_NAME", "value": dagster_bucket_name},
            {"name": "DAGSTER_ENVIRONMENT", "value": stack_info.env_suffix},
            {"name": "DAGSTER_HOSTNAME", "value": dagster_config.require("domain")},
            {"name": "DAGSTER_AIRBYTE_PORT", "value": "443"},
            {
                "name": "DAGSTER_SENSOR_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_MAX_SEND_BYTES",
                "value": "536870912",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "536870912",
            },
            {
                # 2 minutes timeout for loading code locations (handles slow locations)
                "name": "DAGSTER_CODE_SERVER_TIMEOUT_SECONDS",
                "value": "120",
            },
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
            {"name": "dagster-postgresql-secret"},
        ],
    },
    # Dagster daemon (background job scheduler)
    "dagsterDaemon": {
        "annotations": dagster_instance_checksum_annotation,
        "image": dagster_k8s_image_config,
        "env": [
            {
                "name": "DAGSTER_PG_HOST",
                "value": "dagster-pgbouncer.dagster.svc.cluster.local",
            },
            {"name": "DAGSTER_PG_DB", "value": "dagster"},
            {"name": "DAGSTER_BUCKET_NAME", "value": dagster_bucket_name},
            {"name": "DAGSTER_ENVIRONMENT", "value": stack_info.env_suffix},
            {"name": "DAGSTER_HOSTNAME", "value": dagster_config.require("domain")},
            {"name": "DAGSTER_AIRBYTE_PORT", "value": "443"},
            {
                "name": "DAGSTER_SENSOR_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_TIMEOUT_SECONDS",
                "value": "300",
            },
            {
                "name": "DAGSTER_GRPC_MAX_SEND_BYTES",
                "value": "536870912",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "536870912",
            },
            {
                # 2 minutes timeout for loading code locations (handles slow locations)
                "name": "DAGSTER_CODE_SERVER_TIMEOUT_SECONDS",
                "value": "120",
            },
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
            {"name": "dagster-postgresql-secret"},
        ],
        "resources": {
            "requests": {
                "cpu": "500m",
                "memory": "1Gi",
            },
            "limits": {
                "cpu": "2000m",
                "memory": "4Gi",
            },
        },
    },
    # PostgreSQL configuration (using standalone PgBouncer for connection pooling)
    "postgresql": {
        "enabled": False,  # We're using external RDS via standalone PgBouncer
        # Point to the PgBouncer service instead of direct RDS
        "postgresqlHost": "dagster-pgbouncer.dagster.svc.cluster.local",
        "postgresqlDatabase": "dagster",
        "postgresqlPort": 5432,
    },
    # Tell Dagster to use our externally-managed secret
    "generatePostgresqlPasswordSecret": False,
    # Add custom instance ConfigMap with dynamic credential support
    "extraManifests": [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "dagster-instance",
            },
            "data": {
                "dagster.yaml": dagster_instance_yaml,
            },
        }
    ],
    "runLauncher": {
        "type": "K8sRunLauncher",
        "config": {
            "k8sRunLauncher": {
                "imagePullPolicy": "IfNotPresent",
                "loadInclusterConfig": True,
                "envConfigMaps": [],
                "envVars": [
                    "DAGSTER_PG_HOST=dagster-pgbouncer.dagster.svc.cluster.local",
                    "DAGSTER_PG_DB=dagster",
                    f"DAGSTER_BUCKET_NAME={dagster_bucket_name}",
                    f"DAGSTER_ENVIRONMENT={stack_info.env_suffix}",
                    "AWS_DEFAULT_REGION=us-east-1",
                ],
                "envSecrets": [
                    {"name": "dagster-static-secrets"},
                    {"name": "dagster-dbt-secrets"},
                    {"name": "dagster-postgresql-secret"},
                ],
                "volumeMounts": [],
                "volumes": [],
                "jobNamespace": dagster_namespace,
            },
        },
    },
}

# Deploy Dagster using Helm
# Note: Using local vendored chart with JSON schema files removed.
# This works around a Pulumi/Helm SDK bug where schema validation fails
# on external $ref URLs (https://kubernetesjsonschema.dev).
# To update charts, run: ./vendor_charts.sh
dagster_chart_path = str(
    Path(__file__).parent
    / "helm-charts"
    / f"dagster-{DAGSTER_CHART_VERSION}-noschema.tgz"
)
dagster_helm_release = kubernetes.helm.v3.Release(
    f"dagster-helm-release-{stack_info.env_suffix}",
    kubernetes.helm.v3.ReleaseArgs(
        name="dagster",
        namespace=dagster_namespace,
        chart="dagster",
        version=DAGSTER_CHART_VERSION,
        cleanup_on_fail=True,
        values=dagster_helm_values,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://dagster-io.github.io/helm"
        ),
    ),
    opts=ResourceOptions(
        depends_on=[
            dagster_db_secret,
            dagster_static_secrets,
            dagster_dbt_secrets,
            dagster_auth_binding,
            dagster_run_priority_class,
        ]
    ),
)

# Deploy Dagster user code separately - one deployment per code location
dagster_user_code_service_account = kubernetes.core.v1.ServiceAccount(
    f"dagster-user-code-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-user-code",
        namespace=dagster_namespace,
        annotations={
            "eks.amazonaws.com/role-arn": dagster_auth_binding.irsa_role.arn,
        },
        labels=k8s_global_labels.model_dump(),
    ),
)

dagster_user_code_cluster_role_binding = kubernetes.rbac.v1.ClusterRoleBinding(
    f"dagster-user-code-cluster-role-binding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-user-code:cluster-auth",
        labels=k8s_global_labels.model_dump(),
    ),
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        api_group="rbac.authorization.k8s.io",
        kind="ClusterRole",
        name="system:auth-delegator",
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name="dagster-user-code",
            namespace=dagster_namespace,
        ),
    ],
)

dagster_user_code_values = {
    "global": {"serviceAccountName": "dagster-user-code"},
    "deployments": deployments,
    "serviceAccount": {
        "create": False,
        "name": "dagster-user-code",
    },
}

dagster_user_code_chart_path = str(
    Path(__file__).parent
    / "helm-charts"
    / f"dagster-user-deployments-{DAGSTER_CHART_VERSION}-noschema.tgz"
)
dagster_user_code_release = kubernetes.helm.v3.Release(
    f"dagster-user-code-release-{stack_info.env_suffix}",
    kubernetes.helm.v3.ReleaseArgs(
        name="dagster-user-code",
        namespace=dagster_namespace,
        chart="dagster-user-deployments",
        version=DAGSTER_CHART_VERSION,
        cleanup_on_fail=True,
        disable_openapi_validation=True,
        values=dagster_user_code_values,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://dagster-io.github.io/helm"
        ),
    ),
    opts=ResourceOptions(
        depends_on=[
            dagster_static_secrets,
            dagster_dbt_secrets,
            dagster_db_secret,
            dagster_user_code_service_account,
            dagster_user_code_cluster_role_binding,
            dagster_helm_release,
            aws_profile_configmap,
            edxorg_gcp_secret,
        ]
    ),
)

# Pod Disruption Budget for run workers
dagster_run_worker_pdb = kubernetes.policy.v1.PodDisruptionBudget(
    f"dagster-run-worker-pdb-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-run-worker-pdb",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    spec=kubernetes.policy.v1.PodDisruptionBudgetSpecArgs(
        max_unavailable=0,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={
                "app.kubernetes.io/component": "run_worker",
            },
        ),
    ),
    opts=ResourceOptions(depends_on=[dagster_helm_release, dagster_user_code_release]),
)

# APISix route configuration
dagster_tls_secret_name = "dagster-tls-pair"  # pragma: allowlist secret # noqa: S105
cert_manager_certificate = OLCertManagerCert(
    f"dagster-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="dagster",
        k8s_namespace=dagster_namespace,
        k8s_labels=k8s_global_labels.model_dump(),
        create_apisixtls_resource=True,
        dest_secret_name=dagster_tls_secret_name,
        dns_names=[dagster_config.require("domain")],
    ),
)

dagster_apisix_route = OLApisixRoute(
    f"dagster-apisix-route-{stack_info.env_suffix}",
    route_configs=[
        OLApisixRouteConfig(
            route_name="dagster",
            priority=10,
            hosts=[dagster_config.require("domain")],
            paths=["/*"],
            backend_service_name="dagster-dagster-webserver",
            backend_service_port=3000,
            plugins=[
                OLApisixPluginConfig(
                    **dagster_oidc_resources.get_full_oidc_plugin_config(
                        unauth_action="auth"
                    )
                ),
            ],
        ),
    ],
    k8s_namespace=dagster_namespace,
    k8s_labels=k8s_global_labels.model_dump(),
    opts=ResourceOptions(
        depends_on=[
            dagster_helm_release,
            dagster_user_code_release,
            dagster_oidc_resources,
        ]
    ),
)

# Exports
export(
    "dagster_app",
    {
        "rds_host": dagster_db.db_instance.address,
        "namespace": dagster_namespace,
        "helm_release": dagster_helm_release.name,
        "user_code_release": dagster_user_code_release.name,
        "service_name": "dagster-dagster-webserver",
        "irsa_role_arn": dagster_auth_binding.irsa_role.arn,
        "domain": dagster_config.require("domain"),
    },
)
export(
    "dbt_artifacts",
    {
        "s3_bucket": dagster_bucket_name,
        "s3_prefix": "openmetadata/dbt-artifacts",
    },
)
