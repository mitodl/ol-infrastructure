"""Pulumi project for deploying Dagster to Kubernetes using Helm.

This deployment uses:
- The official Dagster Helm chart for the control plane (webserver, daemon)
- The mitodl/mono-dagster image for user code deployments
- The data EKS cluster
- Existing RDS PostgreSQL instance for Dagster's metadata storage
- S3 for compute logs and I/O manager storage
- Vault for secrets management
- APISix ingress with OpenID Connect for authentication
"""

from pathlib import Path

import pulumi_consul as consul
import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import DAGSTER_CHART_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.consul import get_consul_provider
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
stack_info = parse_stack()

# Config
dagster_config = Config("dagster")
vault_config = Config("vault")

# Stack references
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

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
    service=Services.dagster,
    ou=BusinessUnit.data,
    stack=stack_info,
)

aws_account = get_caller_identity()
consul_provider = get_consul_provider(stack_info)
consul_security_groups = consul_stack.require_output("security_groups")
dagster_namespace = "dagster"

# Verify namespace exists in the cluster
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(dagster_namespace, ns)
)

# Stack references for accessing other application databases
mitxonline_stack = StackReference(f"applications.edxapp.mitxonline.{stack_info.name}")
mitxonline_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.mitxonline.{stack_info.name}"
)
residential_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.mitx.{stack_info.name}"
)
xpro_stack = StackReference(f"applications.edxapp.xpro.{stack_info.name}")
xpro_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.xpro.{stack_info.name}"
)

dagster_bucket_name = f"dagster-{dagster_environment}"
s3_tracking_logs_buckets = [
    f"{edxapp_deployment}-{stack_info.env_suffix}-edxapp-tracking"
    for edxapp_deployment in ("mitxonline", "mitx", "mitx-staging", "xpro")
]
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
            f"arn:aws:s3:::{bucket_name}" for bucket_name in s3_tracking_logs_buckets
        ]
        + [f"arn:aws:s3:::{bucket_name}/*" for bucket_name in s3_tracking_logs_buckets],
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
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
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
dagster_runtime_bucket = s3.Bucket(
    dagster_bucket_name,
    bucket=dagster_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
    server_side_encryption_configuration={
        "rule": {
            "applyServerSideEncryptionByDefault": {
                "sseAlgorithm": "aws:kms",
            },
        },
    },
)

# Bucket to store gcs import of edxorg course tarballs
edxorg_courses_bucket_name = f"edxorg-{stack_info.env_suffix}-edxapp-courses"
edxorg_courses_bucket = s3.Bucket(
    edxorg_courses_bucket_name,
    bucket=edxorg_courses_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
    server_side_encryption_configuration={
        "rule": {
            "applyServerSideEncryptionByDefault": {
                "sseAlgorithm": "aws:kms",
            },
        },
    },
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
rds_defaults["use_blue_green"] = False
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

# Keep Consul service registration for database
dagster_db_consul_node = Node(
    "dagster-instance-db-node",
    name="dagster-postgres-db",
    address=dagster_db.db_instance.address,
    datacenter=dagster_environment,
    opts=consul_provider,
)

dagster_db_consul_service = Service(
    "dagster-instance-db-service",
    node=dagster_db_consul_node.name,
    name="dagster-db",
    port=dagster_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="dagster-instance-db",
            interval="10s",
            name="dagster-instance-id",
            timeout="60s",
            status="passing",
            tcp=dagster_db.db_instance.address.apply(
                lambda address: f"{address}:{dagster_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)


# Keep Consul keys for pipeline configurations (may still be referenced)
consul.Keys(
    "dagster-consul-template-data",
    keys=[
        consul.KeysKeyArgs(
            path="dagster/postgresql-host",
            value=dagster_db.db_instance.address,
        ),
        consul.KeysKeyArgs(
            path="dagster/dagster-bucket-name",
            value=f"dagster-data-{stack_info.env_suffix}",
        ),
        consul.KeysKeyArgs(
            path="dagster/server-address",
            value=get_config("dagster:domain"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/shared/env-suffix",
            value=stack_info.env_suffix,
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mit-open/bucket-name",
            value=get_config("dagster:edx_pipeline_mit_open_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mit-open/postgres-db/hostname",
            value=get_config("dagster:edx_pipeline_mit_open_postgres_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/bucket-name",
            value=get_config("dagster:edx_pipeline_xpro_edx_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/mysql-db/hostname",
            value=xpro_stack.require_output("edxapp")["mariadb"],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/mongodb-forum/uri",
            value=xpro_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/xpro-purpose",
            value=get_config("dagster:edx_pipeline_xpro_edx_xpro_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/xpro-course-bucket-name",
            value=get_config("dagster:edx_pipeline_xpro_edx_course_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/postgres-db/name",
            value=get_config("dagster:edx_pipeline_micromasters_postgres_db_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/postgres-db/hostname",
            value=get_config("dagster:edx_pipeline_micromasters_postgres_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/bucket-name",
            value=get_config("dagster:edx_pipeline_micromasters_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline-mitx-online/edx-course-bucket",
            value=get_config("dagster:edx_pipeline_mitx_online_edx_course_bucket"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitx-online/purpose",
            value=get_config("dagster:edx_pipeline_mitx_online_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/residential/mongodb/uri",
            value=residential_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/residential/mysql-db/hostname",
            value=get_config("dagster:edx_pipeline_residential_mysql_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/mysql-db/hostname",
            value=mitxonline_stack.require_output("edxapp")["mariadb"],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/mongodb/uri",
            value=mitxonline_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/purpose",
            value=get_config("dagster:edx_pipeline_xpro_edx_xpro_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/edx-course-bucket-name",
            value=get_config("dagster:edx_pipeline_mitxonline_edx_course_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitx-enrollments/bucket-name",
            value=get_config("dagster:edx_pipeline_mitx_enrollments_bucket_name"),
        ),
    ],
    opts=consul_provider,
)

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
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="dagster",
        vault_sync_service_account_name="dagster-vault",
        k8s_labels=k8s_global_labels,
        parliament_config=parliament_config,
    )
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
        path="dagster",
        refresh_after="1m",
        templates={
            "DAGSTER_AIRBYTE_AUTH": '{{ printf "dagster:%s" (get .Secrets "dagster_unhashed_password") }}',  # noqa: E501
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
        revoke_on_delete=True,
        role_name="app",
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
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
        oidc_session_cookie_lifetime=60 * 20160,  # 14 days
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/dagster",
        vaultauth=dagster_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[dagster_auth_binding]),
)

# ConfigMap for pipeline environment variables
dagster_pipeline_env_configmap = kubernetes.core.v1.ConfigMap(
    f"dagster-pipeline-env-configmap-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="dagster-pipeline-env",
        namespace=dagster_namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={
        "DAGSTER_PG_HOST": dagster_db.db_instance.address,
        "DAGSTER_PG_DB_NAME": "dagster",
        "DAGSTER_BUCKET_NAME": dagster_bucket_name,
        "DAGSTER_ENVIRONMENT": stack_info.env_suffix,
        "DAGSTER_HOSTNAME": dagster_config.require("domain"),
        "DAGSTER_AIRBYTE_PORT": "443",
        "AWS_DEFAULT_REGION": "us-east-1",
    },
)

# Dagster Helm chart values
dagster_helm_values = {
    "global": {
        "serviceAccountName": "dagster",
        "postgresqlSecretName": "dagster-postgresql-secret",  # pragma: allowlist secret
    },
    "serviceAccount": {
        "create": True,
        "name": "dagster",
        "annotations": {
            "eks.amazonaws.com/role-arn": dagster_auth_binding.irsa_role.arn,
        },
    },
    # Dagster webserver (UI)
    "dagsterWebserver": {
        "replicas": 2,
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
            "enabled": True,
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
            "enabled": True,
            "httpGet": {
                "path": "/server_info",
                "port": 3000,
            },
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "timeoutSeconds": 10,
            "failureThreshold": 3,
        },
        "env": {
            "DAGSTER_K8S_PIPELINE_RUN_NAMESPACE": dagster_namespace,
            "DAGSTER_K8S_PIPELINE_RUN_ENV_CONFIGMAP": "dagster-pipeline-env",
        },
        "envConfigMaps": [
            {"name": "dagster-pipeline-env"},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
        ],
    },
    # Dagster daemon (background job scheduler)
    "dagsterDaemon": {
        "replicas": 1,
        "serviceAccount": {
            "name": "dagster-daemon",
            "create": True,
            "annotations": {
                "eks.amazonaws.com/role-arn": dagster_auth_binding.irsa_role.arn,
            },
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
        "env": {
            "DAGSTER_K8S_PIPELINE_RUN_NAMESPACE": dagster_namespace,
            "DAGSTER_K8S_PIPELINE_RUN_ENV_CONFIGMAP": "dagster-pipeline-env",
        },
        "envConfigMaps": [
            {"name": "dagster-pipeline-env"},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
        ],
    },
    # PostgreSQL configuration (using external RDS)
    "postgresql": {
        "enabled": False,  # We're using external RDS
    },
    "runLauncher": {
        "type": "K8sRunLauncher",
        "config": {
            "k8sRunLauncher": {
                "envConfigMaps": [{"name": "dagster-pipeline-env"}],
                "envSecrets": [
                    {"name": "dagster-static-secrets"},
                    {"name": "dagster-dbt-secrets"},
                    {"name": "dagster-postgresql-secret"},
                ],
                "jobNamespace": dagster_namespace,
                "serviceAccountName": "dagster-user-deployments",
            },
        },
    },
    # User code deployments (mitodl/mono-dagster image)
    "dagsterUserDeployments": {
        "enabled": True,
        "enableSubchart": True,
        "deployments": [
            {
                "name": "dagster-user-code",
                "image": {
                    "repository": dagster_config.get("docker_repo_name")
                    or "mitodl/mono-dagster",
                    "tag": dagster_config.get("docker_image_tag") or "latest",
                    "pullPolicy": "Always",
                },
                "dagsterApiGrpcArgs": [
                    "-m",
                    "dagster_user_code",
                ],
                "port": 3030,
                "replicas": 2,
                "serviceAccount": {
                    "name": "dagster-user-deployments",
                    "create": True,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": dagster_auth_binding.irsa_role.arn,  # noqa: E501
                    },
                },
                "resources": {
                    "requests": {
                        "cpu": "1000m",
                        "memory": "2Gi",
                    },
                    "limits": {
                        "cpu": "4000m",
                        "memory": "8Gi",
                    },
                },
                "env": {
                    "DAGSTER_CURRENT_IMAGE": Output.concat(
                        dagster_config.get("docker_repo_name") or "mitodl/mono-dagster",
                        ":",
                        dagster_config.get("docker_image_tag") or "latest",
                    ),
                    "DAGSTER_SENSOR_GRPC_TIMEOUT_SECONDS": "300",
                },
                "envConfigMaps": [
                    {"name": "dagster-pipeline-env"},
                ],
                "envSecrets": [
                    {"name": "dagster-static-secrets"},
                    {"name": "dagster-dbt-secrets"},
                ],
            }
        ],
    },
}

# Deploy Dagster using Helm
dagster_helm_release = kubernetes.helm.v3.Release(
    f"dagster-helm-release-{stack_info.env_suffix}",
    kubernetes.helm.v3.ReleaseArgs(
        name="dagster",
        version=DAGSTER_CHART_VERSION,
        namespace=dagster_namespace,
        chart="dagster",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://dagster-io.github.io/helm",
        ),
        cleanup_on_fail=True,
        values=dagster_helm_values,
    ),
    opts=ResourceOptions(
        depends_on=[
            dagster_pipeline_env_configmap,
            dagster_db_secret,
            dagster_static_secrets,
            dagster_dbt_secrets,
            dagster_auth_binding,
        ]
    ),
)

# APISix route configuration
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
    opts=ResourceOptions(depends_on=[dagster_helm_release, dagster_oidc_resources]),
)

# Exports
export(
    "dagster_app",
    {
        "rds_host": dagster_db.db_instance.address,
        "namespace": dagster_namespace,
        "helm_release": dagster_helm_release.name,
        "service_name": "dagster-dagster-webserver",
        "irsa_role_arn": dagster_auth_binding.irsa_role.arn,
        "domain": dagster_config.require("domain"),
    },
)
