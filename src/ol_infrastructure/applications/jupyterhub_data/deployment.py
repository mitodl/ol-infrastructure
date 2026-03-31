"""Provision the JupyterHub Data deployment.

Key differences from the existing jupyterhub/deployment.py:
- No OLApisixOIDCResources — JupyterHub owns auth via GenericOAuthenticator
- APISIX route provides TLS termination and WebSocket proxying only
- Injects TRINO_TOKEN from the user's OIDC access token via KubeSpawner pre_spawn_hook
- JUPYTERHUB_CRYPT_KEY is required for auth state (access/refresh token) encryption
- Uses EFS dynamic storage (efs-sc) for per-user home directories
- No course image pre-puller
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import JUPYTERHUB_CHART_VERSION, MARIMO_JUPYTERLAB_VERSION
from ol_infrastructure.applications.jupyterhub.values import get_authenticator_config
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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
from ol_infrastructure.lib.ol_types import StackInfo
from ol_infrastructure.lib.vault import postgres_role_statements

# JupyterHub KubeSpawner pre_spawn_hook: injects the user's Keycloak OIDC access token
# as TRINO_TOKEN so notebooks can authenticate against Starburst Galaxy per-user
# without any manual credential management. Token refresh is handled by JupyterHub
# automatically (Keycloak sessions are 2 hours idle per ol-data-platform realm config).
_PRE_SPAWN_HOOK = """
async def pre_spawn_hook(spawner):
    auth_state = await spawner.user.get_auth_state()
    if auth_state and auth_state.get("access_token"):
        spawner.environment["TRINO_TOKEN"] = auth_state["access_token"]

c.KubeSpawner.pre_spawn_hook = pre_spawn_hook
"""

# KubeSpawner profile list: lets users choose their compute tier at launch time.
_PROFILE_LIST = f"""
c.KubeSpawner.profile_list = [
    {{
        "display_name": "Standard",
        "description": "2 CPU / 8 GB — general data analysis",
        "default": True,
        "kubespawner_override": {{
            "cpu_guarantee": 0.5,
            "cpu_limit": 2,
            "mem_guarantee": "2G",
            "mem_limit": "8G",
            "image": "ghcr.io/mitodl/marimo-jupyterlab:{MARIMO_JUPYTERLAB_VERSION}",
        }},
    }},
    {{
        "display_name": "Large",
        "description": "4 CPU / 32 GB — heavy computation",
        "kubespawner_override": {{
            "cpu_guarantee": 1,
            "cpu_limit": 4,
            "mem_guarantee": "8G",
            "mem_limit": "32G",
            "image": "ghcr.io/mitodl/marimo-jupyterlab:{MARIMO_JUPYTERLAB_VERSION}",
        }},
    }},
]
"""

# Common JupyterHub settings shared across the hub configuration
_COMMON_HUB_CONFIG = """
import os

c.JupyterHub.db_url = os.environ["DATABASE_URL"]
c.Authenticator.allow_all = True

# Read GenericOAuthenticator credentials from Vault-injected environment variables
c.GenericOAuthenticator.client_id = os.environ.get("OAUTH_CLIENT_ID", "")
c.GenericOAuthenticator.client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
"""


def provision_jupyterhub_data_deployment(  # noqa: PLR0913
    stack_info: StackInfo,
    domain_name: str,
    trino_host: str,
    namespace: str,
    vault_config: Config,
    db_config: OLPostgresDBConfig,
    jupyterhub_data_db: OLAmazonDB,
    cluster_stack: StackReference,
    service_trust_role: OLEKSTrustRole,
    application_labels: dict[str, str],
    k8s_global_labels: dict[str, str],
    service_account_name: str,
    jupyterhub_data_config: Config,
) -> kubernetes.helm.v3.Release:
    """Provision JupyterHub data Helm release with GenericOAuthenticator and Trino token injection."""  # noqa: E501
    base_name = "jupyterhub-data"
    env_name = stack_info.name
    vault_policy_hcl = (
        Path(__file__).parent.joinpath("jupyterhub_data_policy.hcl").read_text()
    )
    authenticator_class = (
        jupyterhub_data_config.get("authenticator_class") or "generic-oauth"
    )
    keycloak_base_url = jupyterhub_data_config.get("keycloak_base_url") or ""
    keycloak_realm = jupyterhub_data_config.get("keycloak_realm") or "ol-data-platform"

    # Vault Policy
    vault_policy = vault.Policy(
        f"ol-{base_name}-vault-policy-{stack_info.env_suffix}",
        name=base_name,
        policy=vault_policy_hcl,
    )

    # Vault K8S Auth Backend Role — bound to the jupyterhub-data namespace.
    # The marimo namespace is also bound so published-app pods can read their
    # service-account Trino credentials via VSO.
    vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{base_name}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=base_name,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[namespace, "marimo"],
        token_policies=[vault_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=base_name,
            namespace=namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[vault_k8s_auth_backend_role],
        ),
    )

    # VaultStaticSecret: syncs ol-marimo-client OIDC credentials into the namespace
    # so the hub pod can read OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET from env vars.
    oidc_secret_name = "jupyterhub-data-oidc-secret"  # noqa: S105  # pragma: allowlist secret
    oidc_static_secret = OLVaultK8SSecret(
        f"{base_name}-oidc-static-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=oidc_secret_name,
            exclude_raw=True,
            excludes=[".*"],
            includes=["client_id", "client_secret"],
            labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v2",
            name=f"{base_name}-oidc-secret",
            namespace=namespace,
            path="sso/marimo",
            refresh_after="1h",
            templates={
                "OAUTH_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "OAUTH_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',  # pragma: allowlist secret  # noqa: E501
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=[vault_k8s_resources]),
    )

    # VaultStaticSecret: syncs the 32-byte hex JUPYTERHUB_CRYPT_KEY used to encrypt
    # auth state (access + refresh tokens) stored in the JupyterHub database.
    crypt_key_secret_name = "jupyterhub-data-crypt-key"  # noqa: S105  # pragma: allowlist secret
    crypt_key_static_secret = OLVaultK8SSecret(
        f"{base_name}-crypt-key-static-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=crypt_key_secret_name,
            exclude_raw=True,
            excludes=[".*"],
            includes=["JUPYTERHUB_CRYPT_KEY"],
            labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v2",
            name=f"{base_name}-crypt-key",
            namespace=namespace,
            path="jupyterhub-data/crypt-key",
            refresh_after="24h",
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=[vault_k8s_resources]),
    )

    # Vault Database Backend and dynamic credentials secret
    vault_backend_config = OLVaultPostgresDatabaseConfig(
        db_name=db_config.db_name,
        mount_point=f"postgres-{base_name}",
        db_admin_username=db_config.username,
        db_admin_password=db_config.password.get_secret_value(),
        db_host=jupyterhub_data_db.db_instance.address,
        role_statements=postgres_role_statements,
    )

    app_vault_backend = OLVaultDatabaseBackend(
        vault_backend_config,
        opts=ResourceOptions(depends_on=[jupyterhub_data_db]),
    )

    creds_secret_name = f"{base_name}-db-creds"
    app_db_creds_dynamic_secret = OLVaultK8SSecret(
        f"{base_name}-app-db-creds-vaultdynamicsecret",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name=f"{base_name}-app-db-creds",
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=creds_secret_name,
            exclude_raw=True,
            labels=k8s_global_labels,
            mount=app_vault_backend.db_mount.path,
            namespace=namespace,
            path="creds/app",
            templates={
                "DATABASE_URL": (
                    f'postgresql://{{{{ get .Secrets "username" }}}}'
                    f':{{{{ get .Secrets "password" }}}}'
                    f"@{db_config.instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:"
                    f"{DEFAULT_POSTGRES_PORT}/{db_config.db_name}"
                )
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=app_vault_backend),
    )

    # APISIX shared plugins (request-id, etc.) — no OIDC plugin; JupyterHub owns auth
    shared_plugins = OLApisixSharedPlugins(
        name=f"ol-{base_name}-external-service-apisix-plugins",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name=base_name,
            resource_suffix="ol-shared-plugins",
            k8s_namespace=namespace,
            k8s_labels=application_labels,
            enable_defaults=True,
        ),
    )

    # TLS certificate for the JupyterHub domain
    api_tls_secret_name = f"api-{base_name}-tls-pair"
    OLCertManagerCert(
        f"ol-{base_name}-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name=base_name,
            k8s_namespace=namespace,
            k8s_labels=application_labels,
            create_apisixtls_resource=True,
            dest_secret_name=api_tls_secret_name,
            dns_names=[domain_name],
        ),
    )

    # APISIX route: TLS termination + WebSocket proxying. No openid-connect plugin.
    OLApisixRoute(
        name=f"ol-{base_name}-k8s-apisix-route-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=application_labels,
        route_configs=[
            OLApisixRouteConfig(
                route_name=base_name,
                priority=0,
                shared_plugin_config_name=shared_plugins.resource_name,
                plugins=[],
                hosts=[domain_name],
                paths=["/*"],
                backend_service_name="proxy-public",
                backend_service_port="http",
                websocket=True,
            ),
        ],
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Kubernetes ServiceAccount annotated with the IRSA role ARN so that
    # single-user pods can read S3 and Glue without long-lived credentials.
    kubernetes.core.v1.ServiceAccount(
        f"jupyterhub-data-service-account-{namespace}-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=service_account_name,
            namespace=namespace,
            labels=k8s_global_labels,
            annotations={
                "eks.amazonaws.com/role-arn": service_trust_role.role.arn,
            },
        ),
        automount_service_account_token=False,
    )

    auth_config = get_authenticator_config(
        {
            "authenticator_class": authenticator_class,
            "keycloak_base_url": keycloak_base_url,
            "keycloak_realm": keycloak_realm,
            "login_service": "MIT OL Data Platform",
            "username_claim": "preferred_username",
        }
    )

    return kubernetes.helm.v3.Release(
        f"{base_name}-{env_name}-application-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            chart="jupyterhub",
            cleanup_on_fail=True,
            version=JUPYTERHUB_CHART_VERSION,
            namespace=namespace,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://hub.jupyter.org/helm-chart/",
            ),
            name=base_name,
            skip_crds=False,
            values={
                "ingress": {
                    "enabled": False,
                },
                "cull": {
                    "enabled": True,
                    "every": 300,
                    "timeout": 14400,  # 4 hours idle before culling
                    "maxAge": 0,  # no absolute session limit
                    "users": False,
                },
                "proxy": {
                    "service": {
                        "type": "ClusterIP",
                    },
                    "chp": {
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "64Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                        "pdb": {"enabled": True},
                    },
                },
                "scheduling": {
                    "podPriority": {"enabled": True},
                    "userScheduler": {
                        "enabled": True,
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "64Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    },
                },
                "hub": {
                    "db": {"type": "postgres"},
                    "extraEnv": [
                        {
                            "name": "DATABASE_URL",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": creds_secret_name,
                                    "key": "DATABASE_URL",
                                }
                            },
                        },
                        {
                            "name": "JUPYTERHUB_CRYPT_KEY",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": crypt_key_secret_name,
                                    "key": "JUPYTERHUB_CRYPT_KEY",
                                }
                            },
                        },
                        {
                            "name": "OAUTH_CLIENT_ID",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": oidc_secret_name,
                                    "key": "OAUTH_CLIENT_ID",
                                }
                            },
                        },
                        {
                            "name": "OAUTH_CLIENT_SECRET",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": oidc_secret_name,
                                    "key": "OAUTH_CLIENT_SECRET",  # pragma: allowlist secret  # noqa: E501
                                }
                            },
                        },
                    ],
                    "extraConfig": {
                        "hubDataConfig.py": (
                            _COMMON_HUB_CONFIG + _PRE_SPAWN_HOOK + _PROFILE_LIST
                        ),
                    },
                    "config": auth_config,
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "512Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                    "pdb": {"enabled": True},
                },
                "prePuller": {
                    "continuous": {"enabled": False},
                    "hook": {"enabled": False},
                },
                "singleuser": {
                    "serviceAccountName": service_account_name,
                    "image": {
                        "name": "ghcr.io/mitodl/marimo-jupyterlab",
                        "tag": MARIMO_JUPYTERLAB_VERSION,
                        "pullPolicy": "Always",
                    },
                    "cmd": ["jupyterhub-singleuser"],
                    "startTimeout": 300,
                    "networkPolicy": {"enabled": False},
                    "extraEnv": {
                        "TRINO_HOST": trino_host,
                        "TRINO_PORT": "443",
                        "JUPYTERHUB_SINGLEUSER_APP": (
                            "jupyter_server.serverapp.ServerApp"
                        ),
                    },
                    "storage": {
                        "type": "dynamic",
                        "capacity": "5Gi",
                        "dynamic": {
                            "storageClass": "efs-sc",
                        },
                    },
                    "cloudMetadata": {"blockWithIptables": False},
                    # Resource defaults; overridden per-user by KubeSpawner profile_list
                    "memory": {"limit": "8G", "guarantee": "2G"},
                    "cpu": {"guarantee": 0.5},
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[
                app_db_creds_dynamic_secret,
                oidc_static_secret,
                crypt_key_static_secret,
            ],
        ),
    )
