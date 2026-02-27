"""Deploy Lightdash to EKS."""

import json
from pathlib import Path
from string import Template

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import LIGHTDASH_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
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
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

setup_vault_provider()
lightdash_config = Config("lightdash")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
vault_infra_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

data_vpc = network_stack.require_output("data_vpc")
lightdash_env = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(tags={"OU": "data", "Environment": lightdash_env})

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
lightdash_namespace = "lightdash"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(lightdash_namespace, ns)
)

k8s_labels = K8sAppLabels(
    application=Application.lightdash,
    product=Product.data,
    service=Services.lightdash,
    source_repository="https://lightdash.github.io/helm-charts",
    ou=BusinessUnit.data,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

aws_account = get_caller_identity()
lightdash_domain = lightdash_config.require("domain")

########################################
# S3 bucket + IRSA for Lightdash       #
########################################

lightdash_bucket_name = f"ol-lightdash-{stack_info.env_suffix}"
lightdash_policy_document = {
    "Version": IAM_POLICY_VERSION,
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
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject*",
            ],
            "Resource": [
                f"arn:aws:s3:::{lightdash_bucket_name}",
                f"arn:aws:s3:::{lightdash_bucket_name}/*",
            ],
        },
    ],
}

lightdash_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="lightdash",
        namespace=lightdash_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=lightdash_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("lightdash_server_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="lightdash",
        vault_sync_service_account_names="lightdash-vault",
        k8s_labels=k8s_labels,
    )
)

lightdash_bucket_config = S3BucketConfig(
    bucket_name=lightdash_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerEnforced",
    tags=aws_config.tags,
)

lightdash_bucket = OLBucket(
    "lightdash-storage-bucket",
    config=lightdash_bucket_config,
)

########################################
# Vault KV-v2 mount                    #
########################################

lightdash_vault_mount = vault.Mount(
    "lightdash-vault-configuration-secrets-mount",
    path="secret-lightdash",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration secrets for Lightdash.",
    opts=ResourceOptions(delete_before_replace=True),
)
lightdash_vault_kv_path = lightdash_vault_mount.path

lightdash_secrets = (
    read_yaml_secrets(Path(f"lightdash/data.{stack_info.env_suffix}.yaml")) or {}
)
if lightdash_secrets:
    vault.kv.SecretV2(
        "lightdash-vault-secret-app",
        mount=lightdash_vault_kv_path,
        name="app",
        data_json=json.dumps(lightdash_secrets),
    )

########################################
# RDS PostgreSQL + Vault DB backend    #
########################################

k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]

lightdash_db_security_group = ec2.SecurityGroup(
    "lightdash-rds-security-group",
    name_prefix=f"lightdash-rds-{lightdash_env}-",
    description="Grant access to RDS from Lightdash",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            cidr_blocks=k8s_pod_subnet_cidrs,
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
    tags=aws_config.merged_tags({"Name": f"lightdash-rds-{lightdash_env}"}),
    vpc_id=data_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
lightdash_db_config = OLPostgresDBConfig(
    instance_name=f"ol-lightdash-db-{stack_info.env_suffix}",
    password=lightdash_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[lightdash_db_security_group],
    tags=aws_config.tags,
    db_name="lightdash",
    **rds_defaults,
)
lightdash_db = OLAmazonDB(lightdash_db_config)

lightdash_role_statements = postgres_role_statements.copy()
lightdash_role_statements["app"] = postgres_role_statements["app"].copy()
lightdash_role_statements["app"]["create"] = [
    *postgres_role_statements["app"]["create"],
    # Ensure graphile_worker schema exists and app role has full access
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF NOT EXISTS (
              SELECT FROM pg_catalog.pg_namespace
              WHERE nspname = 'graphile_worker') THEN
              CREATE SCHEMA graphile_worker AUTHORIZATION "${app_name}";
           END IF;
        END
        $$do$$;"""
    ),
    Template(
        """GRANT ALL ON SCHEMA graphile_worker TO "${app_name}" WITH GRANT OPTION;"""
    ),
    Template("""SET ROLE "${app_name}";"""),
    Template(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA graphile_worker
        GRANT ALL ON TABLES TO "${app_name}" WITH GRANT OPTION;"""
    ),
    Template(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA graphile_worker
        GRANT ALL ON SEQUENCES TO "${app_name}" WITH GRANT OPTION;"""
    ),
    Template("""RESET ROLE;"""),
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF EXISTS (
              SELECT FROM pg_catalog.pg_extension
              WHERE  extname = 'uuid-ossp') THEN
                  RAISE NOTICE 'Extension "uuid-ossp" already exists. Skipping.';
           ELSE
              BEGIN
                 CREATE EXTENSION "uuid-ossp";
              EXCEPTION
                 WHEN duplicate_object THEN
                    RAISE NOTICE 'Extension "uuid-ossp" already created. Skipping.';
              END;
           END IF;
        END
        $$do$$;"""
    ),
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF EXISTS (
              SELECT FROM pg_catalog.pg_extension
              WHERE  extname = 'citext') THEN
                  RAISE NOTICE 'Extension "citext" already exists. Skipping.';
           ELSE
              BEGIN
                 CREATE EXTENSION citext;
              EXCEPTION
                 WHEN duplicate_object THEN
                    RAISE NOTICE 'Extension "citext" already created. Skipping.';
              END;
           END IF;
        END
        $$do$$;"""
    ),
    Template(
        """
        DO
        $$do$$
        BEGIN
           IF EXISTS (
              SELECT FROM pg_catalog.pg_extension
              WHERE  extname = 'ltree') THEN
                  RAISE NOTICE 'Extension "ltree" already exists. Skipping.';
           ELSE
              BEGIN
                 CREATE EXTENSION ltree;
              EXCEPTION
                 WHEN duplicate_object THEN
                    RAISE NOTICE 'Extension "ltree" already created. Skipping.';
              END;
           END IF;
        END
        $$do$$;"""
    ),
]

lightdash_vault_db_config = OLVaultPostgresDatabaseConfig(
    db_name=lightdash_db_config.db_name,
    mount_point=f"{lightdash_db_config.engine}-lightdash",
    db_admin_username=lightdash_db_config.username,
    db_admin_password=lightdash_config.require("db_password"),
    db_host=lightdash_db.db_instance.address,
    role_statements=lightdash_role_statements,
)
lightdash_db_vault_backend = OLVaultDatabaseBackend(lightdash_vault_db_config)

vault_k8s_resources = lightdash_app.vault_k8s_resources

########################################
# Vault -> Kubernetes secret sync      #
########################################

# Dynamic DB credentials
db_creds_secret_name = "lightdash-db-creds"  # pragma: allowlist secret  # noqa: S105
db_creds_secret = OLVaultK8SSecret(
    f"lightdash-db-creds-{stack_info.env_suffix}",
    resource_config=OLVaultK8SDynamicSecretConfig(
        name="lightdash-db-creds",
        namespace=lightdash_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=db_creds_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=lightdash_vault_db_config.mount_point,
        path="creds/app",
        templates={
            "PGUSER": "{{ .Secrets.username }}",
            "PGPASSWORD": "{{ .Secrets.password }}",
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
)

# Static app secret (LIGHTDASH_SECRET, etc.)
app_secret_name = "lightdash-app-secret"  # pragma: allowlist secret  # noqa: S105
app_secret = OLVaultK8SSecret(
    f"lightdash-app-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="lightdash-app-secret",
        namespace=lightdash_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=app_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=lightdash_vault_kv_path,
        mount_type="kv-v2",
        path="app",
        templates={
            "LIGHTDASH_SECRET": '{{ get .Secrets "lightdash_secret" }}',
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
)

# OIDC (Keycloak) credentials
oidc_secret_name = "lightdash-oidc-secret"  # pragma: allowlist secret  # noqa: S105
oidc_secret = OLVaultK8SSecret(
    f"lightdash-oidc-config-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="lightdash-oidc-config",
        namespace=lightdash_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=oidc_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/lightdash",
        templates={
            "AUTH_OKTA_OAUTH_CLIENT_ID": '{{ get .Secrets "client_id" }}',
            "AUTH_OKTA_OAUTH_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            "AUTH_OKTA_OAUTH_ISSUER": '{{ get .Secrets "issuer_url" }}',
            "AUTH_OKTA_DOMAIN": '{{ get .Secrets "domain" }}',
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
)

########################################
# Lightdash Helm chart on Kubernetes   #
########################################

lightdash_chart = kubernetes.helm.v3.Release(
    "lightdash-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="lightdash",
        chart="lightdash",
        version=LIGHTDASH_CHART_VERSION,
        namespace=lightdash_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://lightdash.github.io/helm-charts",
        ),
        values={
            # Use external RDS — disable bundled Postgres
            "postgresql": {"enabled": False},
            # Non-sensitive configuration
            "configMap": Output.all(
                db_host=lightdash_db.db_instance.address,
            ).apply(
                lambda kwargs: {
                    "SITE_URL": f"https://{lightdash_domain}",
                    "TRUST_PROXY": "true",
                    "SECURE_COOKIES": "true",
                    "PGHOST": kwargs["db_host"],
                    "PGPORT": str(DEFAULT_POSTGRES_PORT),
                    "PGDATABASE": lightdash_db_config.db_name,
                    "PGSSL": "true",
                    "PGSSLMODE": "no-verify",
                    "S3_REGION": "us-east-1",
                    "S3_BUCKET": lightdash_bucket_name,
                    # Use IRSA (EKS pod identity via metadata service)
                    "S3_USE_CREDENTIALS_FROM": "ecs",
                    "AUTH_DISABLE_PASSWORD_AUTHENTICATION": "true",  # pragma: allowlist secret # noqa: E501
                }
            ),
            # Override chart's default LIGHTDASH_SECRET; real value comes from extraEnv
            "secrets": {"LIGHTDASH_SECRET": ""},
            # Load sensitive values from Vault-synced K8s secrets
            "extraEnv": [
                {
                    "name": "PGUSER",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_creds_secret_name,
                            "key": "PGUSER",
                        }
                    },
                },
                {
                    "name": "PGPASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_creds_secret_name,
                            "key": "PGPASSWORD",
                        }
                    },
                },
                {
                    "name": "LIGHTDASH_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": app_secret_name,
                            "key": "LIGHTDASH_SECRET",
                        }
                    },
                },
                {
                    "name": "AUTH_OKTA_OAUTH_CLIENT_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": oidc_secret_name,
                            "key": "AUTH_OKTA_OAUTH_CLIENT_ID",
                        }
                    },
                },
                {
                    "name": "AUTH_OKTA_OAUTH_CLIENT_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": oidc_secret_name,
                            "key": "AUTH_OKTA_OAUTH_CLIENT_SECRET",
                        }
                    },
                },
                {
                    "name": "AUTH_OKTA_OAUTH_ISSUER",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": oidc_secret_name,
                            "key": "AUTH_OKTA_OAUTH_ISSUER",
                        }
                    },
                },
                {
                    "name": "AUTH_OKTA_DOMAIN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": oidc_secret_name,
                            "key": "AUTH_OKTA_DOMAIN",
                        }
                    },
                },
            ],
            # Scheduler also needs secrets
            "schedulerExtraEnv": [
                {
                    "name": "PGUSER",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_creds_secret_name,
                            "key": "PGUSER",
                        }
                    },
                },
                {
                    "name": "PGPASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_creds_secret_name,
                            "key": "PGPASSWORD",
                        }
                    },
                },
                {
                    "name": "LIGHTDASH_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": app_secret_name,
                            "key": "LIGHTDASH_SECRET",
                        }
                    },
                },
            ],
            # Enable the scheduler for scheduled deliveries
            "scheduler": {"enabled": True},
            # Service account with IRSA annotation for S3 access
            "serviceAccount": {
                "create": True,
                "name": "lightdash",
                "annotations": {
                    "eks.amazonaws.com/role-arn": lightdash_app.irsa_role.arn.apply(
                        lambda arn: f"{arn}"
                    ),
                },
            },
            "podLabels": k8s_global_labels,
            # Disable chart's ingress; use Gateway API below
            "ingress": {"enabled": False},
        },
    ),
    opts=ResourceOptions(
        depends_on=[
            db_creds_secret,
            app_secret,
            oidc_secret,
            lightdash_db_vault_backend,
        ]
    ),
)

########################################
# Gateway API routing + TLS certificate #
########################################

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="lightdash",
    namespace=lightdash_namespace,
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https-web",
            hostname=lightdash_domain,
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="lightdash-tls",  # pragma: allowlist secret  # noqa: E501, S106
            certificate_secret_namespace=lightdash_namespace,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="lightdash",
            backend_service_namespace=lightdash_namespace,
            backend_service_port=8080,
            name="lightdash-https-root",
            listener_name="https-web",
            hostnames=[lightdash_domain],
            port=8443,
            matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
        ),
    ],
)

_gateway = OLEKSGateway(
    "lightdash-gateway",
    gateway_config=gateway_config,
    opts=ResourceOptions(parent=lightdash_chart, depends_on=[lightdash_chart]),
)

export(
    "lightdash",
    {
        "deployment": stack_info.env_prefix,
        "db_host": lightdash_db.db_instance.address,
        "s3_bucket": lightdash_bucket_name,
        "domain": lightdash_domain,
    },
)
