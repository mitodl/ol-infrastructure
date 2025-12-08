"""Function for provisioning a JupyterHub deployment
with values consistently derived from its name.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import JUPYTERHUB_CHART_VERSION
from ol_infrastructure.applications.jupyterhub.values import (
    get_authenticator_config,
    get_prepuller_config_for_images,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
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
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.ol_types import StackInfo
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements


def provision_jupyterhub_deployment(  # noqa: PLR0913
    stack_info: StackInfo,
    jupyterhub_deployment_config: Config,
    vault_config: Config,
    db_config: OLPostgresDBConfig,
    jupyterhub_db: OLAmazonDB,
    cluster_stack: StackReference,
    application_labels: dict[str, str],
    k8s_global_labels: dict[str, str],
    extra_images: dict[str, dict[str, str]] | None = None,
    service_account_name: str | None = None,
) -> kubernetes.helm.v3.Release:
    base_name = jupyterhub_deployment_config["name"]
    domain_name = jupyterhub_deployment_config["domain"]
    namespace = jupyterhub_deployment_config["namespace"]
    env_name = f"{stack_info.name}"
    db_name_normalized = base_name.replace("-", "_")
    proxy_port = jupyterhub_deployment_config["proxy_port"]

    # Read configuration files from paths in deployment config
    menu_override_json = (
        Path(__file__)
        .parent.joinpath(jupyterhub_deployment_config["menu_override_file"])
        .read_text()
    )
    disabled_extensions_json = (
        Path(__file__)
        .parent.joinpath(jupyterhub_deployment_config["disabled_extension_file"])
        .read_text()
    )
    extra_config = (
        Path(__file__)
        .parent.joinpath(jupyterhub_deployment_config["extra_config_file"])
        .read_text()
    )
    vault_policy_hcl = (
        Path(__file__)
        .parent.joinpath(jupyterhub_deployment_config["vault_policy_file"])
        .read_text()
    )

    # Derive common configuration values
    apisix_ingress_class = (
        jupyterhub_deployment_config.get("apisix_ingress_class") or "apisix"
    )

    rds_defaults = defaults(stack_info)["rds"]
    rds_defaults["instance_size"] = (
        jupyterhub_deployment_config.get("db_instance_size")
        or rds_defaults["instance_size"]
    )
    rds_defaults["use_blue_green"] = False

    # Vault Policy
    vault_policy = vault.Policy(
        f"ol-{base_name}-vault-policy-{stack_info.env_suffix}",
        name=base_name,
        policy=vault_policy_hcl,
    )

    # Vault K8S Auth Backend Role
    vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{base_name}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=base_name,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )

    # Vault K8S Resources
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
    vault_backend_config = OLVaultPostgresDatabaseConfig(
        db_name=db_config.db_name,
        mount_point=f"{db_config.engine}-{db_config.db_name}",
        db_admin_username=db_config.username,
        db_admin_password=db_config.password.get_secret_value(),
        db_host=jupyterhub_db.db_instance.address,
        role_statements=postgres_role_statements,
    )

    # Vault Database Backend
    app_vault_backend = OLVaultDatabaseBackend(
        vault_backend_config,
        opts=ResourceOptions(depends_on=[jupyterhub_db]),
    )

    # Dynamic Secret for Database Credentials
    creds_secret_name = f"{base_name}-db-creds"
    app_db_creds_dynamic_secret_config = OLVaultK8SDynamicSecretConfig(
        name=f"{base_name}-app-db-creds",
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=creds_secret_name,
        exclude_raw=True,
        labels=k8s_global_labels,
        mount=app_vault_backend.db_mount.path,
        namespace=namespace,
        path="creds/app",
        templates={
            "DATABASE_URL": f'postgresql://{{{{ get .Secrets "username" }}}}'
            f':{{{{ get .Secrets "password" }}}}'
            f"@{db_config.instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:"
            f"{DEFAULT_POSTGRES_PORT}/{db_name_normalized}"
        },
        vaultauth=vault_k8s_resources.auth_name,
    )
    app_db_creds_dynamic_secret = OLVaultK8SSecret(
        f"{base_name}-app-db-creds-vaultdynamicsecret",
        resource_config=app_db_creds_dynamic_secret_config,
        opts=ResourceOptions(depends_on=app_vault_backend),
    )

    # APISIX Shared Plugins
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

    # Certificate
    api_tls_secret_name = f"api-{base_name}-tls-pair"
    OLCertManagerCert(
        f"ol-{base_name}-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name=base_name,
            k8s_namespace=namespace,
            k8s_labels=application_labels,
            create_apisixtls_resource=True,
            apisixtls_ingress_class=apisix_ingress_class,
            dest_secret_name=api_tls_secret_name,
            dns_names=[domain_name],
        ),
    )

    # OIDC Configuration
    # ol-k8s-apisix-olapisixoidcresources-ci
    oidc = OLApisixOIDCResources(
        f"ol-k8s-apisix-{base_name}-olapisixoidcresources-{stack_info.env_suffix}",
        oidc_config=OLApisixOIDCConfig(
            application_name=base_name,
            k8s_labels=application_labels,
            k8s_namespace=namespace,
            oidc_logout_path="hub/logout",
            oidc_post_logout_redirect_uri=f"https://{domain_name}/hub/login",
            oidc_session_cookie_lifetime=60 * 20160,
            oidc_use_session_secret=True,
            oidc_scope="openid email",
            vault_mount="secret-operations",
            vault_path="sso/mitlearn",
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )

    # APISIX Route
    OLApisixRoute(
        name=f"ol-{base_name}-k8s-apisix-route-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=application_labels,
        ingress_class_name=apisix_ingress_class,
        route_configs=[
            OLApisixRouteConfig(
                route_name=base_name,
                priority=0,
                shared_plugin_config_name=shared_plugins.resource_name,
                plugins=[
                    oidc.get_full_oidc_plugin_config(unauth_action="auth"),
                ],
                hosts=[domain_name],
                paths=["/*"],
                backend_service_name="proxy-public",
                backend_service_port="http",
                websocket=True,
            ),
        ],
        opts=ResourceOptions(
            delete_before_replace=True,
        ),
    )

    # JupyterHub Helm Release
    extra_images_list = extra_images or {}
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
                    "timeout": 900,
                    "maxAge": 14400,
                    "users": True,
                },
                "proxy": {
                    "service": {
                        "type": "NodePort",
                        "nodePorts": {
                            "http": proxy_port,
                            "https": 30443,
                        },
                    },
                    "chp": {
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                            "limits": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                        },
                    },
                },
                "scheduling": {
                    "podPriority": {"enabled": True},
                    "userPlaceholder": {
                        "enabled": True,
                        "replicas": jupyterhub_deployment_config.get(
                            "user_placeholder_replicas"
                        )
                        or 4,
                    },
                    "userScheduler": {
                        "enabled": True,
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                            "limits": {
                                "memory": "64Mi",
                            },
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
                        }
                    ],
                    "extraFiles": {
                        "mit_learn_svg": {
                            "mountPath": "/opt/mit_learn.svg",
                            "stringData": Path(__file__)
                            .parent.joinpath("mit_learn.svg")
                            .read_text(),
                        }
                    },
                    "extraConfig": {"dynamicImageConfig.py": extra_config},
                    "config": get_authenticator_config(jupyterhub_deployment_config),
                    "resources": {
                        "requests": {
                            "cpu": "100m",
                            "memory": "256Mi",
                        },
                        "limits": {
                            "memory": "256Mi",
                        },
                    },
                },
                "prePuller": get_prepuller_config_for_images(extra_images_list),
                "singleuser": {
                    "serviceAccountName": service_account_name,
                    "extraFiles": {
                        "menu_override": {
                            "mountPath": (
                                "/opt/conda/share/jupyter/lab/settings/overrides.json"
                            ),
                            "stringData": menu_override_json,
                        },
                        "disabled_extensions": {
                            "mountPath": (
                                "/home/jovyan/.jupyter/labconfig/page_config.json"
                            ),
                            "stringData": disabled_extensions_json,
                        },
                    },
                    "image": {
                        "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com"
                        "/ol-course-notebooks",
                        "tag": "clustering_and_descriptive_ai",
                        "pullPolicy": "Always",
                    },
                    "extraTolerations": [
                        {
                            "key": "ol.mit.edu/gpu_node",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "allowPrivilegeEscalation": True,
                    "cmd": [
                        "jupyterhub-singleuser",
                    ],
                    "startTimeout": 600,
                    "networkPolicy": {
                        "enabled": False,
                    },
                    "memory": {
                        "limit": "2G",
                        "guarantee": "2G",
                    },
                    "cpu": {
                        "guarantee": 0.25,
                    },
                    "storage": {
                        "type": "none",
                    },
                    "extraEnv": {
                        "JUPYTERHUB_SINGLEUSER_APP": (
                            "jupyter_server.serverapp.ServerApp"
                        ),
                        "NOTEBOOK_BUCKET": jupyterhub_deployment_config.get(
                            "notebook_bucket", ""
                        ),
                    },
                    "cloudMetadata": {
                        "blockWithIptables": False,
                    },
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[app_db_creds_dynamic_secret]
        ),
    )
