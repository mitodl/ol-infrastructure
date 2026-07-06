import json
from pathlib import Path
from string import Template
from typing import Any

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
)
from bridge.lib.versions import OPEN_METADATA_VERSION
from bridge.secrets import sops as _bridge_sops
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
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
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

# Resolve the bridge secrets directory once at module level using the sops
# module's own __file__ — the same base path read_yaml_secrets uses internally.
_BRIDGE_SECRETS_DIR = Path(_bridge_sops.__file__).parent

setup_vault_provider()
stack_info = parse_stack()

open_metadata_config = Config("open_metadata")
dns_stack = make_stack_reference(projects.DNS, "default")
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
policy_stack = make_stack_reference(projects.POLICIES, "default")
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)
opensearch_stack = make_stack_reference(
    projects.OPENSEARCH, f"open_metadata.{stack_info.name}"
)
cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")

opensearch_cluster = opensearch_stack.require_output("cluster")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]
open_metadata_environment = f"operations-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": open_metadata_environment},
)

vault_config = Config("vault")

setup_vault_provider(stack_info)

k8s_global_labels = K8sAppLabels(
    application=Application.open_metadata,
    product=Product.data,
    service=Services.open_metadata,
    ou=BusinessUnit.data,
    source_repository="https://github.com/open-metadata/OpenMetadata",
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

aws_account = get_caller_identity()

open_metadata_namespace = "open-metadata"
open_metadata_service_account_name = "openmetadata"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(open_metadata_namespace, ns)
)

open_metadata_database_security_group = ec2.SecurityGroup(
    f"open-metadata-database-security-group-{stack_info.env_suffix}",
    name=f"open-metadata-database-security-group-{stack_info.env_suffix}",
    description="Access control for the open metadata database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                # add k8s ingress?
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to postgres from open metadata servers.",
        ),
        # TODO @Ardiea: switch to use pod-security-groups once implemented  # noqa: FIX002, E501
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster to talk to DB",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    open_metadata_config.get("db_instance_size") or DBInstanceTypes.small.value
)
rds_defaults["enhanced_monitoring_interval"] = 0
rds_defaults["performance_insights_enabled"] = False
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
open_metadata_db_config = OLPostgresDBConfig(
    instance_name=f"open-metadata-db-{stack_info.env_suffix}",
    password=open_metadata_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[open_metadata_database_security_group],
    storage=open_metadata_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    tags=aws_config.tags,
    db_name="open_metadata",
    **rds_defaults,
)
open_metadata_db = OLAmazonDB(open_metadata_db_config)

# Ref: https://docs.open-metadata.org/latest/deployment/kubernetes/eks
open_metadata_role_statements = postgres_role_statements.copy()
open_metadata_role_statements["app"]["create"].append(
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF EXISTS (
              SELECT FROM pg_catalog.pg_extension
              WHERE  extname = 'pgcrypto') THEN
                  RAISE NOTICE 'Extension "pgcrypto" already exists. Skipping.';
           ELSE
              BEGIN   -- nested block
                 CREATE EXTENSION pgcrypto;
              EXCEPTION
                 WHEN duplicate_object THEN
                    RAISE NOTICE 'Extension "pgcrypto" was just created by a concurrent transaction. Skipping.';
              END;
           END IF;
        END
        $$do$$;"""  # noqa: E501
    )
)
open_metadata_role_statements["app"]["create"].append(
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF EXISTS (
              SELECT FROM pg_catalog.pg_extension
              WHERE  extname = 'pg_trgm') THEN
                  RAISE NOTICE 'Extension "pg_trgm" already exists. Skipping.';
           ELSE
              BEGIN   -- nested block
                 CREATE EXTENSION pg_trgm;
              EXCEPTION
                 WHEN duplicate_object THEN
                    RAISE NOTICE 'Extension "pg_trgm" was just created by a concurrent transaction. Skipping.';
              END;
           END IF;
        END
        $$do$$;"""  # noqa: E501
    )
)

open_metadata_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=open_metadata_db_config.db_name,
    mount_point=f"{open_metadata_db_config.engine}-open-metadata",
    db_admin_username=open_metadata_db_config.username,
    db_admin_password=open_metadata_config.get("db_password"),
    db_host=open_metadata_db.db_instance.address,
    role_statements=open_metadata_role_statements,
)
open_metadata_db_vault_backend = OLVaultDatabaseBackend(
    open_metadata_db_vault_backend_config,
    opts=ResourceOptions(delete_before_replace=True, parent=open_metadata_db),
)

# Create a vault policy and associate it with an auth backend role
# on the vault k8s cluster auth endpoint
open_metadata_vault_policy = vault.Policy(
    "open-metadata-vault-policy",
    name="open-metadata",
    policy=Path(__file__).parent.joinpath("open_metadata_policy.hcl").read_text(),
)
open_metadata_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "open-metadata-vault-k8s-auth-backend-role",
    role_name="open-metadata",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["open-metadata-vault"],
    bound_service_account_namespaces=[open_metadata_namespace],
    token_policies=[open_metadata_vault_policy.name],
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="open-metadata",
    namespace=open_metadata_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=open_metadata_vault_auth_backend_role.role_name,
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[open_metadata_vault_auth_backend_role],
    ),
)

db_creds_secret_name = "pgsql-db-creds"  # noqa: S105  # pragma: allowlist secret
db_creds_secret_config = OLVaultK8SDynamicSecretConfig(
    name="openmetadata-db-creds",
    namespace=open_metadata_namespace,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=db_creds_secret_name,
    labels=k8s_global_labels,
    mount=open_metadata_db_vault_backend_config.mount_point,
    path="creds/app",
    restart_target_kind="Deployment",
    restart_target_name="openmetadata",
    templates={
        "DB_USER": '{{ get .Secrets "username" }}',
        "DB_USER_PASSWORD": '{{ get .Secrets "password" }}',
    },
    vaultauth=vault_k8s_resources.auth_name,
)
db_creds_secret = OLVaultK8SSecret(
    f"open-metadata-{stack_info.name}-db-creds-secret",
    db_creds_secret_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
    ),
)
oidc_config_secret_name = "oidc-config"  # noqa: S105  # pragma: allowlist secret
oidc_config_secret_config = OLVaultK8SStaticSecretConfig(
    name="openmetadata-oidc-config",
    namespace=open_metadata_namespace,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=oidc_config_secret_name,
    labels=k8s_global_labels,
    mount="secret-operations",
    mount_type="kv-v1",
    path="sso/open_metadata",
    restart_target_kind="Deployment",
    restart_target_name="openmetadata",
    templates={
        "AUTHENTICATION_PUBLIC_KEYS": '[http://openmetadata:8585/api/v1/system/config/jwks,{{ get .Secrets "url" }}/protocol/openid-connect/certs]',  # noqa: E501
        "AUTHENTICATION_AUTHORITY": '{{ get .Secrets "url" }}',
        "AUTHENTICATION_CLIENT_ID": '{{ get .Secrets "client_id" }}',
        "OIDC_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
        # Required for confidential OIDC (MCP OAuth SSO handler initialisation).
        # Keycloak discovery endpoint is <realm_url>/.well-known/openid-configuration.
        "OIDC_DISCOVERY_URI": '{{ get .Secrets "url" }}/.well-known/openid-configuration',  # noqa: E501
    },
    vaultauth=vault_k8s_resources.auth_name,
)
oidc_config_secret = OLVaultK8SSecret(
    f"open-metadata-{stack_info.name}-oidc-config-secret",
    oidc_config_secret_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
    ),
)

# The Helm chart's confidential OIDC helper expects a K8s secret named
# "oidc-secrets" with keys openmetadata-oidc-client-id and
# openmetadata-oidc-client-secret. Create it from the same Vault path.
oidc_helm_secret = OLVaultK8SSecret(
    f"open-metadata-{stack_info.name}-oidc-helm-secret",
    OLVaultK8SStaticSecretConfig(
        name="openmetadata-oidc-helm",
        namespace=open_metadata_namespace,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name="oidc-secrets",  # noqa: S106  # pragma: allowlist secret
        labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/open_metadata",
        restart_target_kind="Deployment",
        restart_target_name="openmetadata",
        templates={
            "openmetadata-oidc-client-id": '{{ get .Secrets "client_id" }}',
            "openmetadata-oidc-client-secret": '{{ get .Secrets "client_secret" }}',
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
    ),
)

# Connector credentials for OpenMetadata ingestion pipelines.
# Credentials are read from SOPS-encrypted bridge secrets, written to a
# dedicated Vault mount, and synced into K8s secrets via vault-secrets-operator.
open_metadata_connector_secrets_path = (
    _BRIDGE_SECRETS_DIR / f"open_metadata/secrets.{stack_info.env_suffix}.yaml"
)
open_metadata_connector_secrets: dict[str, Any] = {}
if open_metadata_connector_secrets_path.exists():
    _raw_secrets = read_yaml_secrets(open_metadata_connector_secrets_path)
    if not isinstance(_raw_secrets, dict):
        msg = (
            f"Failed to decrypt {open_metadata_connector_secrets_path}: "
            f"expected a dict but got {type(_raw_secrets).__name__}. "
            "Check that sops can decrypt the file and that AWS KMS is accessible."
        )
        raise TypeError(msg)
    open_metadata_connector_secrets = _raw_secrets

open_metadata_connector_vault_mount = vault.Mount(
    f"open-metadata-connector-vault-mount-{stack_info.env_suffix}",
    description="Static connector credentials for OpenMetadata ingestion pipelines",
    path="secret-openmetadata",
    type="kv",
    options={"version": "2"},
    opts=ResourceOptions(parent=vault_k8s_resources),
)

connector_secrets: list[OLVaultK8SSecret] = []
connector_secret_names: list[str] = []

if open_metadata_connector_secrets:
    open_metadata_connector_vault_secret = vault.generic.Secret(
        f"open-metadata-connector-vault-secret-{stack_info.env_suffix}",
        path="secret-openmetadata/connectors",
        data_json=Output.secret(json.dumps(open_metadata_connector_secrets)),
        opts=ResourceOptions(
            depends_on=[open_metadata_connector_vault_mount],
            parent=vault_k8s_resources,
        ),
    )

    # Each entry maps connector name → K8s env var → Vault template.
    # Uses nested index syntax because secrets are stored as a map-of-maps.
    # Only include connectors whose top-level key exists in the secrets file
    # so stacks without a given connector's credentials don't create a K8s
    # secret with unresolvable Vault template references.
    _all_connector_configs: dict[str, dict[str, str | Output[str]]] = {
        "trino": {
            "OM_TRINO_HOST_PORT": '{{ index .Secrets "trino" "host_port" }}',
            "OM_TRINO_USERNAME": '{{ index .Secrets "trino" "login_email" }}',
            "OM_TRINO_PASSWORD": '{{ index .Secrets "trino" "password" }}',
            "OM_TRINO_CATALOG": '{{ index .Secrets "trino" "catalog" }}',
        },
        "airbyte": {
            "OM_AIRBYTE_HOST_PORT": '{{ index .Secrets "airbyte" "host_port" }}',
            "OM_AIRBYTE_PIPELINE_URL": '{{ index .Secrets "airbyte" "pipeline_url" }}',
        },
        "superset": {
            "OM_SUPERSET_OIDC_REALM_URL": (
                '{{ index .Secrets "superset" "oidc_realm_url" }}'
            ),
            "OM_SUPERSET_OIDC_CLIENT_ID": (
                '{{ index .Secrets "superset" "oidc_client_id" }}'
            ),
            "OM_SUPERSET_OIDC_CLIENT_SECRET": (
                '{{ index .Secrets "superset" "oidc_client_secret" }}'
            ),
        },
    }
    connector_configs = {
        name: templates
        for name, templates in _all_connector_configs.items()
        if name in open_metadata_connector_secrets
    }

    for connector_name, templates in connector_configs.items():
        secret_name = f"om-connector-{connector_name}"
        secret_config = OLVaultK8SStaticSecretConfig(
            name=f"openmetadata-connector-{connector_name}",
            namespace=open_metadata_namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=secret_name,
            labels=k8s_global_labels,
            mount="secret-openmetadata",
            mount_type="kv-v2",
            path="connectors",
            restart_target_kind="Deployment",
            restart_target_name="openmetadata",
            templates=templates,
            vaultauth=vault_k8s_resources.auth_name,
        )
        connector_secret = OLVaultK8SSecret(
            f"open-metadata-{stack_info.name}-connector-{connector_name}-secret",
            secret_config,
            opts=ResourceOptions(
                delete_before_replace=True,
                parent=vault_k8s_resources,
                depends_on=[open_metadata_connector_vault_secret],
            ),
        )
        connector_secrets.append(connector_secret)
        connector_secret_names.append(secret_name)

# OM ships with several system bots, each with its own JWT used by a specific
# workflow type.  All known bots are listed here (SOPS key → OM hyphenated name).
# Any bot whose token appears in the SOPS file gets its own Vault secret and a
# VSO-managed K8s secret (``om-<bot>``).  The K8s secret exposes a single key
# ``OM_BOT_JWT_TOKEN`` so all ingestion scripts can use the same env-var name.
_OM_BOTS: dict[str, str] = {
    "ingestion_bot": "ingestion-bot",
    "lineage_bot": "lineage-bot",
    "profiler_bot": "profiler-bot",
    "data_insight_bot": "data-insight-bot",
}

for _bot_key, _bot_name in _OM_BOTS.items():
    if not open_metadata_connector_secrets.get(_bot_key):
        continue
    _k8s_secret_name = f"om-{_bot_name}"  # pragma: allowlist secret
    vault.generic.Secret(
        f"open-metadata-{_bot_name}-vault-secret-{stack_info.env_suffix}",
        path=f"secret-openmetadata/{_bot_name}",
        data_json=Output.secret(json.dumps(open_metadata_connector_secrets[_bot_key])),
        opts=ResourceOptions(
            depends_on=[open_metadata_connector_vault_mount],
            parent=vault_k8s_resources,
        ),
    )
    OLVaultK8SSecret(
        f"open-metadata-{stack_info.name}-{_bot_name}-secret",
        OLVaultK8SStaticSecretConfig(
            name=f"openmetadata-{_bot_name}",
            namespace=open_metadata_namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=_k8s_secret_name,
            labels=k8s_global_labels,
            mount="secret-openmetadata",
            mount_type="kv-v2",
            path=_bot_name,
            restart_target_kind="Deployment",
            restart_target_name="openmetadata",
            templates={"OM_BOT_JWT_TOKEN": '{{ get .Secrets "token" }}'},
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=vault_k8s_resources,
        ),
    )
    connector_secret_names.append(_k8s_secret_name)

# IRSA trust role granting the OpenMetadata server pod read access to AWS Glue and S3.
# Uses OLEKSTrustRole directly (rather than OLEKSAuthBinding) because Vault K8s
# auth is already managed above via OLVaultK8SResources; OLEKSAuthBinding would
# create a conflicting duplicate Vault auth backend role.
open_metadata_glue_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
                "glue:GetPartition",
                "glue:GetPartitions",
            ],
            "Resource": [
                "arn:aws:glue:*:*:catalog",
                f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
                f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:ListBucket",
            ],
            "Resource": [
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
            ],
        },
    ],
}

open_metadata_glue_iam_policy = iam.Policy(
    f"open-metadata-glue-read-policy-{stack_info.env_suffix}",
    name=f"open-metadata-glue-read-policy-{stack_info.env_suffix}",
    path=f"/ol-applications/open-metadata/open_metadata/{stack_info.env_suffix}/",
    description=(
        "Read-only access to AWS Glue catalog and S3 data lake"
        " for OpenMetadata ingestion pipelines"
    ),
    policy=lint_iam_policy(
        open_metadata_glue_policy_document,
        stringify=True,
        parliament_config={"RESOURCE_EFFECTIVELY_STAR": {}},
    ),
    tags=aws_config.tags,
)

# Grants the server pod access to invoke the Bedrock embedding model used for
# semantic search (see EMBEDDING_PROVIDER below). The DJL provider is unusable
# on the official OpenMetadata image: DJL downloads a glibc-linked PyTorch
# native runtime, but the image is Alpine/musl, so it fails every time with
# `UnsatisfiedLinkError: ... initial-exec TLS resolves to dynamic definition`.
# Unresolved upstream as of 1.13.1 -
# https://github.com/open-metadata/OpenMetadata/issues/29576
open_metadata_bedrock_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": ["arn:aws:bedrock:*::foundation-model/amazon.titan-embed-*"],
        },
    ],
}

open_metadata_bedrock_iam_policy = iam.Policy(
    f"open-metadata-bedrock-embedding-policy-{stack_info.env_suffix}",
    name=f"open-metadata-bedrock-embedding-policy-{stack_info.env_suffix}",
    path=f"/ol-applications/open-metadata/open_metadata/{stack_info.env_suffix}/",
    description=(
        "Allows the OpenMetadata server to invoke the Bedrock Titan embedding"
        " model used for semantic/vector search"
    ),
    policy=lint_iam_policy(
        open_metadata_bedrock_policy_document,
        stringify=True,
        # Parliament's RESOURCE_MISMATCH check doesn't recognize the
        # foundation-model resource type for bedrock:InvokeModel.
        parliament_config={"RESOURCE_EFFECTIVELY_STAR": {}, "RESOURCE_MISMATCH": {}},
    ),
    tags=aws_config.tags,
)

open_metadata_irsa_role = OLEKSTrustRole(
    f"open-metadata-irsa-trust-role-{stack_info.env_suffix}",
    role_config=OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description=(
            "Trust role for OpenMetadata service account"
            " to access AWS Glue and S3 data lake"
        ),
        policy_operator="StringEquals",
        role_name="open-metadata",
        service_account_name=open_metadata_service_account_name,
        service_account_namespace=open_metadata_namespace,
        tags=aws_config.tags,
    ),
)

open_metadata_glue_policy_attachment = iam.RolePolicyAttachment(
    f"open-metadata-glue-policy-attachment-{stack_info.env_suffix}",
    policy_arn=open_metadata_glue_iam_policy.arn,
    role=open_metadata_irsa_role.role.name,
    opts=ResourceOptions(parent=open_metadata_irsa_role),
)

open_metadata_bedrock_policy_attachment = iam.RolePolicyAttachment(
    f"open-metadata-bedrock-policy-attachment-{stack_info.env_suffix}",
    policy_arn=open_metadata_bedrock_iam_policy.arn,
    role=open_metadata_irsa_role.role.name,
    opts=ResourceOptions(parent=open_metadata_irsa_role),
)

# Install the openmetadata helm chart
# https://github.com/mitodl/ol-infrastructure/issues/2680
open_metadata_application = kubernetes.helm.v3.Release(
    f"open-metadata-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="open-metadata",
        chart="openmetadata",
        version=OPEN_METADATA_VERSION,
        namespace=open_metadata_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://helm.open-metadata.org",
        ),
        values={
            "commonLabels": k8s_global_labels,
            "openmetadata": {
                "config": {
                    # Ref: https://docs.open-metadata.org/latest/deployment/security/keycloak/kubernetes
                    "authorizer": {
                        "enabled": True,
                        "className": "org.openmetadata.service.security.DefaultAuthorizer",  # noqa: E501
                        "containerRequestFilter": "org.openmetadata.service.security.JwtFilter",  # noqa: E501
                        "initialAdmins": [
                            "tmacey",
                            "shaidar",
                            "mas48",
                            "cpatti",
                            "qhoque",
                        ],
                        "principalDomain": "mit.edu",
                    },
                    "authentication": {
                        "provider": "custom-oidc",
                        # Ref: https://docs.open-metadata.org/v1.12.x/deployment/security/keycloak/kubernetes
                        "clientType": "confidential",
                        "callbackUrl": f"https://{open_metadata_config.require('domain')}/callback",
                        # publicKeys, authority, clientId loaded from vault via env vars
                        "oidcConfiguration": {
                            "enabled": True,
                            "oidcType": "Keycloak",
                            # callbackUrl and serverUrl must be the external-facing URL
                            # so OpenMetadata builds correct OAuth redirect/issuer URLs
                            # for MCP OAuth.
                            "callbackUrl": f"https://{open_metadata_config.require('domain')}/callback",
                            "serverUrl": f"https://{open_metadata_config.require('domain')}",
                            # discoveryUri loaded from vault via OIDC_DISCOVERY_URI env var.  # noqa: E501
                            # How long before re-authentication is required (seconds).
                            "tokenValidity": "21600",  # 6 hours
                            # Overall session length (seconds).
                            "sessionExpiry": "604800",  # 7 days
                        },
                    },
                    "pipelineServiceClientConfig": {
                        "enabled": True,
                        "type": "k8s",
                        # Tells OpenMetadata its own public API URL, used when
                        # constructing OAuth discovery metadata for MCP OAuth.
                        "metadataApiEndpoint": f"https://{open_metadata_config.require('domain')}/api",
                        "k8s": {
                            # The schema doesn't yet support this value (TMM 2026-02-26)
                            # "namespace": open_metadata_namespace,  # noqa: ERA001
                            # Pin the ingestion SA name (the chart default) so the
                            # cross-stack contract is explicit: the substructure stack
                            # creates this SA + its IRSA role/RBAC under this name.
                            "serviceAccountName": "openmetadata-ingestion",
                            "enableFailureDiagnostics": True,
                            "ingestionImage": f"docker.getcollate.io/openmetadata/ingestion-base:{OPEN_METADATA_VERSION}",  # noqa: E501
                            "useOMJobOperator": True,
                            # The chart's k8s-pipeline-rbac.yaml stamps the shared
                            # `serviceAccount.annotations` (the server IRSA role) onto
                            # the ingestion SA too, which collides with the distinct
                            # ingestion IRSA role applied in the substructure stack.
                            # Disable chart-managed RBAC and own the ingestion SA, its
                            # Role/RoleBinding, and the server pipeline-manager
                            # Role/RoleBinding in substructure/open_metadata instead.
                            "rbac": {"enabled": False},
                        },
                    },
                    "elasticsearch": {
                        "host": opensearch_cluster["endpoint"],
                        "port": DEFAULT_HTTPS_PORT,
                        "scheme": "https",
                    },
                    "database": {
                        "host": open_metadata_db.db_instance.address,
                        "port": open_metadata_db_config.port,
                        "databaseName": open_metadata_db_config.db_name,
                        "driverClass": "org.postgresql.Driver",
                        "dbScheme": "postgresql",
                        # Null out the auth elements in favor of our own
                        # secret pulled in with envFrom below.
                        "auth": {
                            "username": "",
                            "password": {
                                "secretRef": "",
                                "secretKey": "",
                            },
                        },
                    },
                },
            },
            "hpa": {
                "enabled": True,
            },
            # Ref: https://docs.open-metadata.org/v1.13.x/deployment/semantic-search
            # 1.13: hybrid search is now automatic when SEMANTIC_SEARCH_ENABLED=true;
            # the per-query semanticSearch flag is removed. Run the Search Index App
            # after upgrading to populate vector fields in OpenSearch.
            # EMBEDDING_PROVIDER=bedrock (not djl): DJL is unusable on the official
            # Alpine/musl-based server image - it fails to load its glibc-linked
            # PyTorch native runtime every time. Unresolved upstream as of 1.13.1:
            # https://github.com/open-metadata/OpenMetadata/issues/29576
            # Bedrock auth uses the pod's IRSA role (open_metadata_bedrock_iam_policy
            # below) via the AWS default credential provider chain - no API key
            # needed, just AWS_DEFAULT_REGION.
            # LOG_FORMAT=json (1.13) enables JSON-structured Dropwizard server logs.
            "extraEnvs": [
                {
                    "name": "SEMANTIC_SEARCH_ENABLED",
                    "value": "true",
                },
                {
                    "name": "EMBEDDING_PROVIDER",
                    "value": "bedrock",
                },
                # Bedrock's region (parsed by OM from AWS_DEFAULT_REGION per
                # conf/openmetadata.yaml) and the AWS SDK v2 region-provider
                # chain (which the IRSA credentials exchange uses internally,
                # and only recognizes AWS_REGION) read two different env vars -
                # set both rather than assume one covers the other.
                {
                    "name": "AWS_DEFAULT_REGION",
                    "value": "us-east-1",
                },
                {
                    "name": "AWS_REGION",
                    "value": "us-east-1",
                },
                {
                    "name": "LOG_FORMAT",
                    "value": "json",
                },
            ],
            "serviceAccount": {
                "create": True,
                "name": open_metadata_service_account_name,
                "annotations": {
                    "eks.amazonaws.com/role-arn": open_metadata_irsa_role.role.arn,
                },
            },
            "omjobOperator": {
                "enabled": True,
                "image": {
                    "repository": "docker.getcollate.io/openmetadata/omjob-operator",
                    "tag": OPEN_METADATA_VERSION,
                },
            },
            "envFrom": [
                {
                    "secretRef": {
                        "name": db_creds_secret_name,
                    },
                },
                {
                    "secretRef": {
                        "name": oidc_config_secret_name,
                    },
                },
                *[{"secretRef": {"name": name}} for name in connector_secret_names],
            ],
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        parent=vault_k8s_resources,
        delete_before_replace=True,
        depends_on=[
            open_metadata_db,
            db_creds_secret,
            oidc_config_secret,
            oidc_helm_secret,
            open_metadata_irsa_role,
            open_metadata_glue_policy_attachment,
            open_metadata_bedrock_policy_attachment,
            *connector_secrets,
        ],
    ),
)

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="open-metadata",
    labels=k8s_global_labels,
    namespace=open_metadata_namespace,
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https",
            hostname=open_metadata_config.require("domain"),
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="openmetadata-tls",  # cert-manager will create this  # noqa: S106, E501  # pragma: allowlist secret
            certificate_secret_namespace=open_metadata_namespace,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="openmetadata",  # sourced from the helm chart
            backend_service_namespace=open_metadata_namespace,
            backend_service_port=8585,  # sourced from the helm chart
            hostnames=[open_metadata_config.require("domain")],
            name="open-metadata-https",
            listener_name="https",
            port=8443,
        ),
    ],
)

gateway = OLEKSGateway(
    f"open-metadata-{stack_info.name}-gateway",
    gateway_config=gateway_config,
    opts=ResourceOptions(
        parent=open_metadata_application,
        delete_before_replace=True,
    ),
)
