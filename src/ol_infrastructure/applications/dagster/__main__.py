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
    SQL_EXPORTER_VERSION,
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
from ol_infrastructure.lib.aws.iam_helper import (
    IAM_POLICY_VERSION,
    cross_environment_glue_denial,
    data_lake_glue_resources,
)
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
        "Resource": data_lake_glue_resources(stack_info.env_suffix),
    },
    *cross_environment_glue_denial(stack_info.env_suffix),
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
# This stack opts out of all three of the house RDS monitoring defaults
# (monitoring_profile_name="production", enhanced_monitoring_interval=60,
# performance_insights_enabled=True). Two of those opt-outs are still deliberate; the
# third was not, and is why the 2026-08-10 connection exhaustion could not be attributed
# to anything after the fact.
#
# The CloudWatch alarm profile stays disabled for now, but only pending a threshold
# review -- it creates the standard production alarm set against SNS and none of those
# thresholds have been checked against this instance. That is worth doing deliberately
# rather than as a side effect of enabling Performance Insights.
rds_defaults["monitoring_profile_name"] = "disabled"
# Enhanced Monitoring stays off. Unlike Performance Insights it is not free -- the OS
# metric stream bills as CloudWatch Logs ingestion at a 60s interval -- and it answers a
# question we are not asking. The gap here was never host-level CPU/disk; it was which
# queries and wait events were on the database, which is exactly what PI covers.
rds_defaults["enhanced_monitoring_interval"] = 0
# Performance Insights, back on -- production only.
#
# The PgBouncer exporter added in #5426 gives the pool's view of connections; it cannot
# say what those connections were *doing*. With PI off there was no way to attribute the
# 2026-08-10 event -- 4989 connections held for 88 minutes -- to a query, a lock, or a
# checkpoint, and no way to tell a slow-query pileup from a leak the next time it
# happens. PI's DBLoad-by-wait-event is the database-side counterpart to PgBouncer's
# maxwait, which is the signal the pool alerts turn on.
#
# Free, and no reboot. Performance Insights includes 7 days of history and 1M API
# requests/month at no charge, and retention comes from the house production default at
# exactly that 7 days -- raising it, or switching Database Insights from Standard to
# Advanced mode (which forces 15-month retention), is what starts costing money.
# Toggling PI on an instance does not require a reboot, so this applies in place.
#
# Gated on the environment because the surrounding rds_defaults edits are unconditional
# and QA and CI are live stacks with real instances -- ol-etl-db-qa and ol-etl-db-ci
# would both pick this up otherwise. Both support PI and previewed clean, so this is a
# scope choice rather than a compatibility one: the incident being instrumented is on
# production, and the QA/CI instances can be turned on deliberately when something
# actually needs them.
if stack_info.env_suffix == "production":
    rds_defaults["performance_insights_enabled"] = True
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
        # The Vault role is the last path segment (creds/app); revoke the
        # lease on delete so credentials don't outlive this resource.
        revoke_on_delete=True,
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
#
# Production's 6 is not load-derived and the exporter now says so: over seven days the
# busiest replica peaked at 0.09 CPU cores and 35 active clients. PgBouncer is
# single-threaded, which is the usual reason to run several, but nothing here is close
# to needing the parallelism.
#
# The count is not free either. It divides the aggregate budget into per-pod caps, so at
# 6 replicas one pod saturates at 708 while 3540 connections sit unused on the other
# five -- and the binding limit is whichever pod saturates first, not the aggregate.
# That gap is real on QA, which measured 221 active servers on one replica against 4 on
# the other. Production was checked for the same skew before this re-tune and does not
# show it: instantaneous load spreads evenly (1-18 active per pod), so fewer/larger
# replicas is a live option rather than an urgent fix.
#
# Left alone here deliberately. This pass changes min_pool_size, and changing the
# multiplier in the same breath would make the result unattributable -- which is the
# failure mode the whole exercise exists to stop repeating.
pgbouncer_replica_count = dagster_config.get_int("pgbouncer_replica_count") or 2

# Cap the connections PgBouncer can open against RDS, in aggregate across every replica.
#
# Without this, nothing bounds the total. In session pool mode each client pins a server
# connection for its whole session, so the per-pod bound was
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
pgbouncer_ini_template = dagster_db.db_instance.address.apply(
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
            # Clients are cheap -- an accepted client that is waiting for a backend
            # costs a socket, not a connection to RDS -- and this number has to stay
            # well above max_db_connections or a saturated pool refuses clients
            # instead of queueing them. Observed peak is 35 on the busiest replica,
            # so 1500 is not a number the data argues about; it is deliberately far
            # larger than demand.
            "max_client_conn = 1500",
            # One binding ceiling, not three.
            #
            # These were 800 / 150 / 2000, all set before there was any feedback
            # signal. Seven days of 1-minute exporter samples (the exporter landed in
            # #5426) say what they are actually worth:
            #
            #   held server connections   flat 900, every sample, all week
            #   peak server_active        122 aggregate
            #   peak client_active        129 aggregate, 35 on one replica
            #   maxwait                   0 at every sample
            #   clients waiting           peaked at 1
            #
            # 900 was not demand. It is min_pool_size x 6 replicas -- a configured
            # constant, which is exactly what made CloudWatch DatabaseConnections
            # useless as a signal. Nine hundred backends were parked permanently to
            # serve a peak of 122, a 7:1 idle ratio, and the pool never once grew
            # past its own floor.
            #
            # min_pool_size 150 -> 40 keeps the whole measured per-replica peak (35)
            # warm, so nothing in normal operation waits on a connect, and drops the
            # parked total from 900 to 240.
            #
            # default_pool_size and reserve_pool_size were dead numbers: 800 + 2000
            # per pod against a derived cap of 708 means max_db_connections already
            # bound first, so neither value could take effect on production. Rather
            # than pick new guesses, tie default_pool_size to the cap and disable the
            # reserve. With a single database and a single user the two settings act
            # on the same axis, so this makes the derived cap the only ceiling
            # instead of the smallest of three.
            #
            # That property is load-bearing beyond tidiness:
            # DagsterPgBouncerConnectionHeadroom alerts on server connections as a
            # fraction of pgbouncer_databases_max_connections. Any pool number set
            # below the cap would become the real ceiling while the alert kept
            # measuring against the old denominator, and the alert would go quietly
            # dead -- saturation would surface only as clients queueing, which is the
            # symptom the headroom rule exists to get ahead of.
            f"default_pool_size = {pgbouncer_max_db_connections}",
            "min_pool_size = 40",
            "reserve_pool_size = 0",
            # The aggregate ceiling. See the derivation above; this is the
            # only setting here that bounds total backends across replicas,
            # and it converts "exhaust RDS" into "queue inside PgBouncer",
            # which is the failure mode query_wait_timeout below is set high
            # enough to ride out.
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

pgbouncer_config = kubernetes.core.v1.ConfigMap(
    f"dagster-pgbouncer-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pgbouncer-config",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={"pgbouncer.ini.template": pgbouncer_ini_template},
    # The provider replaces this ConfigMap on any data change, and its name is fixed,
    # so the replacement can only be delete-then-create. Make that ordering explicit
    # rather than letting the default create-before-delete attempt fail on
    # "configmaps ... already exists" and recover on the retry.
    opts=ResourceOptions(depends_on=[dagster_db_secret], delete_before_replace=True),
)

# An init container renders the template above into pgbouncer.ini at pod start, so
# PgBouncer only ever reads this config once, when its pod boots. Updating the
# ConfigMap therefore changes nothing about a running pool -- and because the
# Deployment's pod template does not otherwise reference the ConfigMap's content,
# nothing rolls the pods either. A config edit deploys clean, reports success, and
# silently never takes effect.
#
# That is not hypothetical: it has now happened twice. max_db_connections (#5426)
# only became live because someone manually restarted the deployment, and
# query_wait_timeout = 600 (#5454) sat inert in production and QA -- ConfigMaps
# showing 600, all eight running pods still on 0 -- until the pods were restarted
# by hand after the fact. The deadlock protection that change exists to provide was
# absent for as long as nobody thought to check SHOW CONFIG.
#
# Injecting the rendered template's hash into the pod template makes a config edit
# roll the pods the same way an image change would. This mirrors
# dagster_instance_checksum_annotation further down, which exists for exactly this
# reason on the other ConfigMap mounted into these pods (#5373).
#
# The hash is stable across deploys that change nothing: the template embeds
# ${PGUSER}/${PGPASSWORD} as literal placeholders rather than resolved credentials,
# so Vault rotation does not perturb it, and the only inputs that move it are the
# RDS address and the pool settings themselves.
pgbouncer_config_checksum_annotation = {
    "checksum/ol-pgbouncer-config": pgbouncer_ini_template.apply(
        lambda template: hashlib.sha256(template.encode()).hexdigest()
    ),
}

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
                # Rolls these pods when the pgbouncer ConfigMap changes; without it a
                # config edit never reaches the running processes. See the annotation's
                # definition above.
                annotations=pgbouncer_config_checksum_annotation,
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
# These are the metrics the pool-sizing questions turn on. The first pass of answers
# is what the numbers above were re-tuned from; keep watching them, because the
# settings are now supposed to track demand rather than sit above it:
#   pgbouncer_pools_server_active_connections   -- real demand (peak 122 aggregate)
#   pgbouncer_pools_server_idle_connections     -- what min_pool_size parks (was 900)
#   pgbouncer_pools_client_active_connections   -- client demand (peak 129 / 35 a pod)
#   pgbouncer_pools_client_waiting_connections  -- is max_db_connections too tight?
#   pgbouncer_pools_client_maxwait_seconds      -- the number pool tuning turns on,
#                                                  unobtainable from the stats log
#   sum of the above vs. max_connections        -- the 2026-08-10 headroom alert
# Per-pod, not just aggregate: max_db_connections is derived as budget / replica
# count, so a skewed pod saturates while the aggregate still looks calm.
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

# ============================================================================
# SQL exporter against the Dagster metadata database
# ============================================================================
# Dagster OSS exposes no /metrics endpoint and ships no OTel instrumentation, so
# the metadata Postgres is the only place Dagster-domain signal exists at all.
# Everything the cluster knows today is about containers: kube_job_* says a run
# worker exited, never that a run waited nine minutes to start, that the queue is
# 400 deep, or that the sensor daemon stopped heartbeating -- which is precisely
# what nobody could see on 2026-08-10, when the daemon sat 25 minutes behind 178
# run workers and the only visible number was a connection count.
#
# Two properties are load-bearing, not incidental:
#
# 1. It connects DIRECTLY to RDS, not through PgBouncer. The pool's own metrics
#    are the other half of this project, and an exporter polling through the pool
#    would add its connections to the very counts being used to size it.
#
# 2. Every window is bounded by time. How that is made cheap differs per metric,
#    and the rule is that a cost bound may never cut across the dimension the
#    metric is defined on.
#
#    The original pass got this wrong twice over. The windows shipped id-only,
#    sized off an assumed ~53k runs/day. Measured from kube-state-metrics over the
#    seven days to 2026-08-18:
#
#      quiet day        ~143 runs/hour        (3.4k/day)
#      seven-day mean   ~484 runs/hour        (11.6k/day)
#      busiest hour    ~1990 runs
#      busiest 6h      ~7416 runs
#
#    A 14x swing, so a fixed 20000-id window covered anything from ~10 hours to
#    ~6 days depending on when you looked, and `dagster_recent_runs` reported a
#    six-day trailing count while calling itself recent. Adding a lookback fixed
#    the span but introduced a subtler fault: id is CREATION order, and the
#    completion-time metrics filter on update_timestamp, so any run created before
#    the cap but finishing inside the lookback was silently dropped -- long-running
#    and long-queued runs first, which are the ones most worth counting.
#
#    So the cost bound now depends on what the metric measures:
#
#    - Completion-time metrics (dagster_recent_runs, dagster_recent_retried_runs)
#      carry NO id cap. idx_run_range is (status, update_timestamp,
#      create_timestamp), so pairing the status predicate they already need with an
#      update_timestamp range turns them into index-only range scans whose cost
#      tracks rows-in-window rather than table size. This is the exception to "no
#      index on a bare timestamp": there is no such index, but there is a usable
#      composite one the moment status is pinned. Faster as well as correct --
#      0.3ms against 4.6ms for the id-capped version.
#
#    - Creation-ordered metrics keep the id cap, because it agrees with them.
#      dagster_run_wait_to_start_seconds is a cohort of recently CREATED runs
#      (start_time has no index, so it cannot be driven off an event-time index),
#      and job_ticks.id tracks tick timestamp. Cap and predicate move together, so
#      nothing falls between them.
#
#    dagster_id_window_span_seconds reports whether those remaining id caps are
#    truncating: span well above the lookback means the time predicate binds, span
#    at or below it means the cap does and wants raising. It covers only the two
#    id-capped windows, and says so -- a span metric that reported creation age for
#    a completion-time window would be the same mistake wearing a diagnostic hat.
#
# The connection budget is deliberately tiny -- 2 connections, and the DSN sets a
# 10s statement_timeout so no plan this exporter issues can pin a backend. That
# timeout is not decorative: it was confirmed to reach the server by setting it to
# 1ms and watching every query come back 57014.
# Applies only to the creation-ordered run metric (wait-to-start) and the span
# gauge. The completion-time run metrics range-scan idx_run_range instead and take
# no id cap at all -- see the note on dagster_recent_runs. 20000 against a
# busiest-observed 6h of ~7416 creations is 2.7x headroom.
SQL_EXPORTER_RUN_WINDOW = 20000
# Ticks accrue far faster than runs -- the daemon evaluates every sensor on a ~30s
# cadence whether or not it yields a run -- so the same id count buys a much shorter
# span. Sized larger against a shorter lookback for that reason; the span metric
# will say whether 40000 is enough, since unlike the run rate this one has not been
# measured directly.
SQL_EXPORTER_TICK_WINDOW = 40000
# Runs get 6h: long enough that a failure rate is built from thousands of runs at
# peak and hundreds when quiet, short enough that a sustained problem moves it
# within the hour. Ticks get 1h, because a sensor that starts erroring is an acute
# signal and averaging it over six hours only delays noticing.
SQL_EXPORTER_RUN_LOOKBACK = "6 hours"
SQL_EXPORTER_TICK_LOOKBACK = "1 hour"
SQL_EXPORTER_STATEMENT_TIMEOUT_MS = 10000
SQL_EXPORTER_PORT = 9399

# Note on time arithmetic: Dagster stores create_timestamp/timestamp as naive UTC.
# now() is timestamptz, and subtracting a naive timestamp from it silently converts
# using the *session* timezone -- correct only as long as nobody changes it. Every
# such expression below pins it with `now() AT TIME ZONE 'UTC'`. start_time is
# already epoch seconds, so it is compared against extract(epoch FROM ...) instead.
# The S608 suppression on the closing quote is because the only interpolations are
# the integer window constants defined directly above -- nothing user-supplied or
# config-supplied reaches these queries.
sql_exporter_config = f"""\
global:
  scrape_timeout_offset: 500ms
  # A floor on how often the collector may hit the database, independent of how
  # often Prometheus scrapes. The ServiceMonitor asks for 60s, so this only binds
  # if something else starts scraping too.
  min_interval: 30s
  max_connections: 2
  max_idle_connections: 2
  max_connection_lifetime: 10m

# No `target:` block on purpose. The DSN carries a Vault-issued credential, so it
# arrives as an environment variable rather than in this ConfigMap -- and
# SQLEXPORTER_TARGET_DSN is only consulted when the file declares no target at all.
# A target block with the DSN omitted does not fall back to the environment; it
# fails at startup with "missing data_source_name for target".
collectors:
  - collector_name: dagster
    metrics:
      - metric_name: dagster_runs_in_flight
        type: gauge
        help: "Runs that have not reached a terminal state, by status."
        key_labels: [status]
        values: [runs]
        # LEFT JOINed against a VALUES list so every status reports a number even
        # when it has no rows. A GROUP BY alone would drop the series entirely,
        # and "no QUEUED runs" and "the exporter stopped reporting" would look
        # identical to an alert.
        query: |
          SELECT s.status AS status, coalesce(r.n, 0) AS runs
          FROM (VALUES ('QUEUED'), ('NOT_STARTED'), ('STARTING'), ('STARTED'),
                       ('CANCELING')) AS s (status)
          LEFT JOIN (
              SELECT status, count(*) AS n
              FROM runs
              WHERE status IN ('QUEUED', 'NOT_STARTED', 'STARTING', 'STARTED',
                               'CANCELING')
              GROUP BY status
          ) AS r ON r.status = s.status

      - metric_name: dagster_oldest_queued_run_age_seconds
        type: gauge
        help: "Age of the oldest run still in QUEUED, in seconds. 0 when none."
        values: [seconds]
        # Queue depth alone cannot distinguish a healthy burst from a stuck
        # coordinator; this can. Reads only the QUEUED slice of idx_run_status.
        query: |
          SELECT coalesce(
              extract(epoch FROM ((now() AT TIME ZONE 'UTC')
                                  - min(create_timestamp))), 0) AS seconds
          FROM runs
          WHERE status = 'QUEUED'

      - metric_name: dagster_run_wait_to_start_seconds
        type: gauge
        help: >-
          Seconds from run creation to run start, over runs CREATED in the last
          {SQL_EXPORTER_RUN_LOOKBACK} that have started.
        value_label: quantile
        values: [p50, p95, max]
        query_ref: recent_run_waits

      # No _total suffix on any of the three window metrics below. They are gauges
      # over a sliding id window, so they fall as runs leave the window -- and _total
      # tells a consumer this is a monotonic counter, which invites rate() and makes
      # every ordinary decrease look like a counter reset.
      - metric_name: dagster_recent_runs
        type: gauge
        help: >-
          Runs reaching a terminal state in the last {SQL_EXPORTER_RUN_LOOKBACK},
          by status.
        key_labels: [status]
        values: [runs]
        # update_timestamp, not create_timestamp: dagster rewrites it on every
        # status change and sets it to the event time (sql_run_storage.py
        # handle_run_event), so for a terminal status it is when the run finished.
        # A failure rate wants runs that ended in the window, not runs that started
        # in it -- otherwise a long run straddling the boundary is counted before
        # anyone knows how it turned out.
        #
        # No id cap here, deliberately, and this is the one place the "no index on
        # a bare timestamp" rule does not apply. idx_run_range is
        # (status, update_timestamp, create_timestamp), so pairing the status
        # predicate these metrics already need with an update_timestamp range makes
        # update_timestamp the second index column and the whole thing an index-only
        # range scan. Cost then tracks rows-in-window rather than table size, which
        # is the property the id cap was there to provide.
        #
        # Capping by id as well would be actively wrong, not merely redundant: id is
        # creation order, so a run created before the cap but finishing inside the
        # lookback would be dropped. Long-running and long-queued runs are exactly
        # the ones that would go missing, and they are exactly the ones worth
        # counting. Measured on the schema fixture: index-only scan, 573 rows,
        # 0.3ms, against 4.6ms for the id-capped version it replaces. Even a
        # deliberately pathological 30-day lookback stays a range scan (48k rows,
        # 9.4ms) rather than degrading with the table.
        query: |
          SELECT s.status AS status, coalesce(r.n, 0) AS runs
          FROM (VALUES ('SUCCESS'), ('FAILURE'), ('CANCELED')) AS s (status)
          LEFT JOIN (
              SELECT status, count(*) AS n
              FROM runs
              WHERE status IN ('SUCCESS', 'FAILURE', 'CANCELED')
                AND update_timestamp > (now() AT TIME ZONE 'UTC')
                                       - interval '{SQL_EXPORTER_RUN_LOOKBACK}'
              GROUP BY status
          ) AS r ON r.status = s.status

      - metric_name: dagster_recent_retried_runs
        type: gauge
        help: >-
          Runs finishing in the last {SQL_EXPORTER_RUN_LOOKBACK} that carry a
          dagster/retry_number tag.
        values: [runs]
        # run_retries.max_retries = 3 means a job can fail repeatedly and still
        # report SUCCESS, so the run status alone hides chronic flakiness.
        #
        # Driven off idx_run_range the same way, and for the same reason: the cohort
        # has to match dagster_recent_runs exactly or the retry share is a ratio of
        # two different populations.
        query: |
          SELECT count(*) AS runs
          FROM runs AS r
          INNER JOIN run_tags AS rt ON rt.run_id = r.run_id
          WHERE r.status IN ('SUCCESS', 'FAILURE', 'CANCELED')
            AND r.update_timestamp > (now() AT TIME ZONE 'UTC')
                                     - interval '{SQL_EXPORTER_RUN_LOOKBACK}'
            AND rt.key = 'dagster/retry_number'

      - metric_name: dagster_recent_job_ticks
        type: gauge
        help: >-
          Sensor and schedule ticks in the last {SQL_EXPORTER_TICK_LOOKBACK}, by
          type and status.
        key_labels: [tick_type, status]
        values: [ticks]
        # A sensor that starts erroring stops launching runs and says nothing;
        # the only evidence is FAILURE ticks piling up in this table.
        #
        # STARTED is the fourth member of dagster's TickStatus enum and is
        # persisted like the rest. Leaving it out would drop exactly the ticks
        # that are in flight or wedged mid-evaluation -- the interesting ones --
        # from a metric that claims to break ticks down by status.
        query: |
          SELECT c.type AS tick_type, c.status AS status,
                 coalesce(x.n, 0) AS ticks
          FROM (
              SELECT t.type, s.status
              FROM (VALUES ('SENSOR'), ('SCHEDULE'),
                           ('AUTO_MATERIALIZE')) AS t (type)
              CROSS JOIN (VALUES ('SUCCESS'), ('FAILURE'), ('SKIPPED'),
                                 ('STARTED')) AS s (status)
          ) AS c
          LEFT JOIN (
              SELECT type, status, count(*) AS n
              FROM job_ticks
              WHERE id > (SELECT max(id) - {SQL_EXPORTER_TICK_WINDOW}
                          FROM job_ticks)
                AND timestamp > (now() AT TIME ZONE 'UTC')
                                - interval '{SQL_EXPORTER_TICK_LOOKBACK}'
              GROUP BY type, status
          ) AS x ON x.type = c.type AND x.status = c.status

      - metric_name: dagster_daemon_heartbeat_age_seconds
        type: gauge
        help: "Seconds since each Dagster daemon last wrote a heartbeat."
        key_labels: [daemon_type]
        values: [seconds]
        # The 2026-08-10 incident surfaced as a daemon falling 25 minutes behind.
        # This is that symptom, measured directly, off a table with five rows.
        query: |
          SELECT daemon_type,
                 extract(epoch FROM ((now() AT TIME ZONE 'UTC') - timestamp))
                     AS seconds
          FROM daemon_heartbeats

      - metric_name: dagster_id_window_span_seconds
        type: gauge
        help: >-
          Age of the oldest row in each id-capped window, in its own ordering
          dimension. Covers dagster_run_wait_to_start_seconds (runs) and
          dagster_recent_job_ticks (job_ticks) only.
        key_labels: [relation]
        values: [seconds]
        # Says whether an id cap is truncating a window before its lookback does.
        # Span well above the lookback means the time predicate is what binds and
        # the metric covers the period it claims; span at or below the lookback
        # means the cap is biting and wants raising.
        #
        # It can only speak for metrics whose cap and predicate share an ordering
        # dimension, which is now exactly two of them. runs is measured on
        # create_timestamp because the id cap is creation order and the metric it
        # guards, dagster_run_wait_to_start_seconds, is a creation cohort; job_ticks
        # is measured on timestamp, which tracks insertion order.
        #
        # It deliberately says NOTHING about dagster_recent_runs or
        # dagster_recent_retried_runs. Those have no id cap at all any more -- they
        # range-scan idx_run_range by update_timestamp, so there is no truncation to
        # detect. An earlier revision of this metric did claim to cover them, and
        # could not: it reported a creation span while they filtered on completion
        # time, so a healthy-looking span would have sat happily alongside a metric
        # dropping every long-running run. Reviewers caught that; it is the same
        # class of mistake as the one this metric exists to catch, one level up.
        #
        # Reads index ranges the other queries already touch, so it costs a min().
        query: |
          SELECT 'runs' AS relation,
                 coalesce(extract(epoch FROM ((now() AT TIME ZONE 'UTC')
                                              - min(create_timestamp))), 0) AS seconds
          FROM runs
          WHERE id > (SELECT max(id) - {SQL_EXPORTER_RUN_WINDOW} FROM runs)
          UNION ALL
          SELECT 'job_ticks' AS relation,
                 coalesce(extract(epoch FROM ((now() AT TIME ZONE 'UTC')
                                              - min(timestamp))), 0) AS seconds
          FROM job_ticks
          WHERE id > (SELECT max(id) - {SQL_EXPORTER_TICK_WINDOW} FROM job_ticks)

      - metric_name: dagster_relation_bytes
        type: gauge
        help: "On-disk size of the Dagster metadata tables, including indexes."
        key_labels: [relation]
        values: [bytes]
        query: |
          SELECT c.relname AS relation, pg_total_relation_size(c.oid) AS bytes
          FROM pg_class AS c
          INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public'
            AND c.relkind = 'r'
            AND c.relname IN ('runs', 'run_tags', 'event_logs', 'job_ticks',
                              'asset_event_tags', 'asset_keys',
                              'concurrency_slots', 'bulk_actions')

    queries:
      - query_name: recent_run_waits
        # create_timestamp is naive UTC and start_time is epoch seconds, so the
        # difference is taken in epoch space rather than as an interval.
        #
        # This one is a CREATION cohort, and it has to be. There is no index on
        # start_time, so unlike the two metrics above it cannot be driven off an
        # event-time index -- it keeps the id cap. That makes the cap and the
        # predicate the same dimension, since id is creation order: every run the
        # cap admits, create_timestamp also admits, and vice versa. Filtering it by
        # start_time instead would reintroduce the mismatch, silently dropping any
        # run that queued long enough to fall outside the newest ids before starting
        # -- which is precisely the slow-start case this metric exists to measure.
        #
        # The cost is that a run created just before the window and starting just
        # inside it is not counted. That is a real limitation, stated in the help
        # text rather than papered over: this is the wait experienced by runs
        # created recently, not by every run that started recently.
        query: |
          WITH recent AS (
              SELECT start_time - extract(epoch FROM create_timestamp) AS wait
              FROM runs
              WHERE id > (SELECT max(id) - {SQL_EXPORTER_RUN_WINDOW} FROM runs)
                AND create_timestamp > (now() AT TIME ZONE 'UTC')
                                       - interval '{SQL_EXPORTER_RUN_LOOKBACK}'
                AND start_time IS NOT NULL
          )
          SELECT
              coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY wait), 0)
                  AS p50,
              coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY wait), 0)
                  AS p95,
              coalesce(max(wait), 0) AS max
          FROM recent
"""  # noqa: S608

# Read-only Vault credentials, on the same postgres-dagster mount the application
# uses. The readonly role grants SELECT and nothing else, which is all of these
# queries need -- including pg_total_relation_size, confirmed by running the whole
# collector against a role holding only SELECT.
#
# The role is selected by `path` alone. An earlier version of this also passed
# refresh_after, revoke_on_delete and role_name, copied from dagster_db_secret
# above -- all three are inert and were removed rather than left to imply a
# rotation and revocation policy that is not in effect:
#
#   refresh_after     is a real field on the config model, but OLVaultK8SSecret
#                     only renders spec.refreshAfter for STATIC secrets. Moot
#                     regardless: VSO documents the source lease duration as
#                     taking precedence whenever it is greater than 0, and this
#                     mount issues 3-month leases (OLVaultDatabaseConfig
#                     default_ttl = ONE_MONTH_SECONDS * 3).
#   revoke_on_delete  is not a field on the config model at all, so Pydantic
#                     drops it silently. VSO does support spec.revoke; the
#                     component never renders it, so deleting this resource
#                     leaves the lease valid for the rest of its TTL. That gap
#                     is repo-wide, not specific to this resource, and is worth
#                     fixing in OLVaultK8SSecret rather than here.
#   role_name         is not a field either, and VSO has no such concept -- the
#                     Vault role is the last path segment.
dagster_sql_exporter_db_secret = OLVaultK8SSecret(
    f"dagster-k8s-sql-exporter-db-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SDynamicSecretConfig(
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name="dagster-sql-exporter-postgresql-secret",  # pragma: allowlist secret  # noqa: E501, S106
        labels=k8s_global_labels.model_dump(),
        mount="postgres-dagster",
        name="dagster-sql-exporter-postgresql-secret",
        namespace=dagster_namespace,
        path="creds/readonly",
        # The DSN is assembled from these at container start, so a new credential
        # only reaches the process when the pod restarts.
        restart_target_kind="Deployment",
        restart_target_name="dagster-sql-exporter",
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
        templates={
            "PGUSER": "{{ .Secrets.username }}",
            "PGPASSWORD": "{{ .Secrets.password }}",
        },
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

sql_exporter_config_map = kubernetes.core.v1.ConfigMap(
    f"dagster-sql-exporter-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-sql-exporter-config",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={"sql_exporter.yml": sql_exporter_config},
    opts=ResourceOptions(delete_before_replace=True),
)

# sql_exporter reads its config once, at process start. Same trap as pgbouncer.ini
# and dagster_instance.yaml before it: without this, editing a query deploys clean,
# reports success, and changes nothing about the running exporter. See the comment
# on pgbouncer_config_checksum_annotation for the two times that actually happened.
sql_exporter_config_checksum_annotation = {
    "checksum/ol-sql-exporter-config": hashlib.sha256(
        sql_exporter_config.encode()
    ).hexdigest(),
}

sql_exporter_deployment = kubernetes.apps.v1.Deployment(
    f"dagster-sql-exporter-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-sql-exporter",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        # One replica, deliberately. A second would double every gauge's cardinality
        # by pod label while measuring the same database, and there is nothing here
        # to make highly available -- a gap in metrics is a gap in metrics.
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={
                "component": "sql-exporter",
                **k8s_global_labels.model_dump(),
            },
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={
                    "component": "sql-exporter",
                    **k8s_global_labels.model_dump(),
                },
                annotations=sql_exporter_config_checksum_annotation,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="sql-exporter",
                        image=(f"burningalchemist/sql_exporter:{SQL_EXPORTER_VERSION}"),
                        args=[
                            "-config.file=/etc/sql_exporter/sql_exporter.yml",
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGUSER",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="dagster-sql-exporter-postgresql-secret",
                                        key="PGUSER",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGPASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="dagster-sql-exporter-postgresql-secret",
                                        key="PGPASSWORD",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="SQLEXPORTER_TARGET_NAME",
                                value="dagster",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="SQLEXPORTER_TARGET_COLLECTORS",
                                value="dagster",
                            ),
                            # $(VAR) is expanded by Kubernetes from the two secret
                            # vars declared above it, so the credential never lands
                            # in the ConfigMap or in this repo. Same mechanism as
                            # GCLOUD_FM_COLLECTOR_ID in substructure/aws/eks/grafana.
                            #
                            # This is a URL, so the password has to be URL-safe.
                            # Vault's default database password generator emits
                            # [A-Za-z0-9-] only and no password_policy is set on this
                            # mount, so it is -- but that is an assumption worth
                            # knowing about if anyone ever sets one.
                            #
                            # options=--statement_timeout is the libpq --name=value
                            # form on purpose: the more common `-c name=value` form
                            # is mangled in transit and the server rejects it with
                            # "invalid command-line argument for server process: -c".
                            kubernetes.core.v1.EnvVarArgs(
                                name="SQLEXPORTER_TARGET_DSN",
                                value=dagster_db.db_instance.address.apply(
                                    lambda addr: (
                                        "postgres://$(PGUSER):$(PGPASSWORD)@"
                                        f"{addr}:{DEFAULT_POSTGRES_PORT}/dagster"
                                        "?sslmode=require&options=--statement_timeout"
                                        f"%3D{SQL_EXPORTER_STATEMENT_TIMEOUT_MS}"
                                    )
                                ),
                            ),
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="metrics",
                                container_port=SQL_EXPORTER_PORT,
                                protocol="TCP",
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="config",
                                mount_path="/etc/sql_exporter",
                                read_only=True,
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "10m",
                                "memory": "32Mi",
                            },
                            limits={
                                "memory": "128Mi",
                            },
                        ),
                        # /healthz, not /metrics. /metrics fails whenever the
                        # database is unreachable, which is exactly when this
                        # exporter should stay up and report up=0 rather than
                        # crash-loop and report nothing.
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthz",
                                port=SQL_EXPORTER_PORT,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                            failure_threshold=3,
                        ),
                    ),
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="config",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name="dagster-sql-exporter-config",
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[dagster_sql_exporter_db_secret, sql_exporter_config_map]
    ),
)

sql_exporter_service = kubernetes.core.v1.Service(
    f"dagster-sql-exporter-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-sql-exporter",
        namespace=dagster_namespace,
        labels={
            "component": "sql-exporter",
            **k8s_global_labels.model_dump(),
        },
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={
            "component": "sql-exporter",
            **k8s_global_labels.model_dump(),
        },
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="metrics",
                port=SQL_EXPORTER_PORT,
                target_port=SQL_EXPORTER_PORT,
                protocol="TCP",
            ),
        ],
    ),
    opts=ResourceOptions(depends_on=[sql_exporter_deployment]),
)

# 60s is the resolution these questions need -- a queue that is 400 deep for two
# minutes matters, a single scrape does not -- and it keeps the collector's
# min_interval of 30s from ever serving a cached result.
sql_exporter_service_monitor = kubernetes.apiextensions.CustomResource(
    f"dagster-sql-exporter-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-sql-exporter",
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
                "component": "sql-exporter",
                **k8s_global_labels.model_dump(),
            },
        },
        "namespaceSelector": {"matchNames": [dagster_namespace]},
        "endpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "scheme": "http",
                "interval": "60s",
                "scrapeTimeout": "30s",
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
    opts=ResourceOptions(depends_on=[sql_exporter_service]),
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
# Production sets 80 and QA 20 in stack config, both sized against their own
# connection budgets. The 100 here is only a fallback for stacks that set
# nothing; a stack on a small instance class should set its own value rather
# than inherit this one.
dagster_max_concurrent_runs = dagster_config.get_int("max_concurrent_runs") or 100

# event_log_storage's pool_size/max_overflow, same reasoning as
# max_concurrent_runs above: sized against the environment's PgBouncer
# per-pod cap, so it has to be a stack config value rather than a literal in
# dagster_instance.yaml. Production sets 100+50 and QA 30+60, each derived
# against its own cap (see the rationale in dagster_instance.yaml).
#
# The 10+10 fallback here is the pre-incident size. Treat a stack sitting on it
# as unsized, not as measured and fine: QA sat here for a day after Production
# was fixed, on the assumption it had not shown the failure, while its daemon
# logged QueuePool timeouts continuously the whole time.
#
# `or 10` would be wrong here: max_overflow=0 is a legitimate, deliberately
# conservative stack choice (forbid burst connections entirely), and `0 or 10`
# would silently discard it. get_int() returns None when the key is unset, so
# check for that explicitly instead.
dagster_event_log_pool_size = dagster_config.get_int("event_log_pool_size")
if dagster_event_log_pool_size is None:
    dagster_event_log_pool_size = 10

dagster_event_log_max_overflow = dagster_config.get_int("event_log_max_overflow")
if dagster_event_log_max_overflow is None:
    dagster_event_log_max_overflow = 10

# Custom Dagster instance ConfigMap with dynamic credentials support
# Note: We create this before the Helm release so it gets proper ownership
dagster_instance_yaml = (
    Path(__file__)
    .parent.joinpath("dagster_instance.yaml")
    .read_text()
    .replace("MAX_CONCURRENT_RUNS", str(dagster_max_concurrent_runs))
    .replace("EVENT_LOG_POOL_SIZE", str(dagster_event_log_pool_size))
    .replace("EVENT_LOG_MAX_OVERFLOW", str(dagster_event_log_max_overflow))
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
        # Every connection the daemon makes goes to one destination tuple -- the
        # PgBouncer ClusterIP on 5432 -- so its concurrent connection ceiling is
        # the size of the pod's ephemeral port range, and nothing else in the
        # cluster shares those ports because each pod has its own netns. The
        # kernel default of 32768-60999 is 28232 ports, and on 2026-08-18 the
        # daemon consumed all of them: TIME_WAIT is a fixed 60s, so the 331
        # connects/second the NullPool storages were doing parked ~20k sockets
        # at steady state and any burst pushed the total past the range, after
        # which connect() returns EADDRNOTAVAIL.
        #
        # The pooled storage classes in dagster_instance.yaml are the actual fix
        # for that churn; this widens the range to 55296 ports so the pod has
        # room to absorb a burst regardless, and so a future regression degrades
        # instead of failing outright. It is deliberately not a substitute --
        # doubling the range only doubles the rate that exhausts it.
        #
        # net.ipv4.ip_local_port_range has been a Kubernetes safe sysctl since
        # 1.27 (the cluster runs 1.36), so it needs no kubelet allowlist and
        # passes the baseline Pod Security Standard.
        "podSecurityContext": {
            "sysctls": [
                {"name": "net.ipv4.ip_local_port_range", "value": "10240 65535"},
            ],
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
