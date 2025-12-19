from pathlib import Path
from string import Template

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
)
from bridge.lib.versions import OPEN_METADATA_VERSION
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
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
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

open_metadata_config = Config("open_metadata")
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.open_metadata.{stack_info.name}"
)
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

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

k8s_global_labels = K8sGlobalLabels(
    service=Services.open_metadata,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

aws_account = get_caller_identity()

open_metadata_namespace = "open-metadata"
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
    bound_service_account_names=["*"],
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
                        "callbackUrl": f"https://{open_metadata_config.require('domain')}/callback",
                        # To be loaded from vault via env vars
                        # publicKeys
                        # authority
                        # clientId
                    },
                    "pipelineServiceClientConfig": {
                        "enabled": False,
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
            ],
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        parent=vault_k8s_resources,
        delete_before_replace=True,
        depends_on=[open_metadata_db, db_creds_secret, oidc_config_secret],
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
