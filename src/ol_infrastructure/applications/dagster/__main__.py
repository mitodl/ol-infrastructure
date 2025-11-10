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

import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity, s3

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import DAGSTER_CHART_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
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
    ecr_image_uri,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

# Config
dagster_config = Config("dagster")
vault_config = Config("vault")
apisix_ingress_class = dagster_config.get("apisix_ingress_class") or "apisix"

# Stack references
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
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
dagster_pipeline_buckets = s3_tracking_logs_buckets + mitlearn_app_buckets
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
        irsa_service_account_name=["dagster", "dagster-user-code"],
        vault_sync_service_account_names=[
            "dagster",
            "dagster-vault",
            "dagster-user-code",
        ],
        k8s_labels=k8s_global_labels,
        parliament_config=parliament_config,
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
        # Map Vault's fields to both Dagster Helm chart format and environment variables
        templates={
            "postgresql-password": "{{ .Secrets.password }}",
            "DAGSTER_PG_PASSWORD": "{{ .Secrets.password }}",
            "DAGSTER_PG_USER": "{{ .Secrets.username }}",
        },
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
]

# Build deployments list for user code
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
        "strategy": {
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
            "timeoutSeconds": 3,
            "failureThreshold": 60,
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
                "value": "268435456",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "268435456",
            },
            {"name": "DAGSTER_PG_HOST", "value": dagster_db.db_instance.address},
            {"name": "DAGSTER_PG_DB", "value": "dagster"},
            {"name": "DAGSTER_BUCKET_NAME", "value": dagster_bucket_name},
            {"name": "DAGSTER_ENVIRONMENT", "value": stack_info.env_suffix},
            {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
            {"name": "DAGSTER_VAULT_ROLE", "value": "dagster"},
        ],
        "envSecrets": [
            {"name": "dagster-static-secrets"},
            {"name": "dagster-dbt-secrets"},
            {"name": "dagster-postgresql-secret"},
        ],
    }

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
    deployments.append(deployment)

# Custom Dagster instance ConfigMap with dynamic credentials support
# Note: We create this before the Helm release so it gets proper ownership
dagster_instance_yaml = (
    Path(__file__).parent.joinpath("dagster_instance.yaml").read_text()
)

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
            {"name": "DAGSTER_PG_HOST", "value": dagster_db.db_instance.address},
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
                "value": "268435456",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "268435456",
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
        "env": [
            {"name": "DAGSTER_PG_HOST", "value": dagster_db.db_instance.address},
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
                "value": "268435456",
            },
            {
                "name": "DAGSTER_GRPC_MAX_RX_BYTES",
                "value": "268435456",
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
    # PostgreSQL configuration (using external RDS)
    "postgresql": {
        "enabled": False,  # We're using external RDS
        "postgresqlHost": dagster_db.db_instance.address,
        "postgresqlDatabase": "dagster",
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
                "envVars": dagster_db.db_instance.address.apply(
                    lambda db_host: [
                        f"DAGSTER_PG_HOST={db_host}",
                        "DAGSTER_PG_DB=dagster",
                        f"DAGSTER_BUCKET_NAME={dagster_bucket_name}",
                        f"DAGSTER_ENVIRONMENT={stack_info.env_suffix}",
                        "AWS_DEFAULT_REGION=us-east-1",
                    ]
                ),
                "envSecrets": [
                    {"name": "dagster-static-secrets"},
                    {"name": "dagster-dbt-secrets"},
                    {"name": "dagster-postgresql-secret"},
                ],
                "jobNamespace": dagster_namespace,
            },
        },
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
            dagster_db_secret,
            dagster_static_secrets,
            dagster_dbt_secrets,
            dagster_auth_binding,
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

# Use local chart to avoid schema validation issues with external URLs
# The chart is fetched via bin/fetch-dagster-user-deployments-chart.sh which
# removes values.schema.json that references https://kubernetesjsonschema.dev
# (those URLs return 404 in Concourse CI environment due to network restrictions)
dagster_user_deployments_chart_path = str(
    Path(__file__).parent.parent.parent.parent.parent
    / "charts"
    / "dagster-user-deployments"
)

dagster_user_code_release = kubernetes.helm.v3.Release(
    f"dagster-user-code-release-{stack_info.env_suffix}",
    kubernetes.helm.v3.ReleaseArgs(
        name="dagster-user-code",
        namespace=dagster_namespace,
        chart=dagster_user_deployments_chart_path,
        cleanup_on_fail=True,
        disable_openapi_validation=True,
        values=dagster_user_code_values,
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
        apisixtls_ingress_class=apisix_ingress_class,
        dest_secret_name=dagster_tls_secret_name,
        dns_names=[dagster_config.require("domain")],
    ),
)

dagster_apisix_route = OLApisixRoute(
    f"dagster-apisix-route-{stack_info.env_suffix}",
    ingress_class_name=apisix_ingress_class,
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
