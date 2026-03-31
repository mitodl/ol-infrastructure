"""Marimo Data application stack — published notebook applications.

This stack manages the marimo-operator and the APISIX route that exposes
published (run-mode) MarimoNotebook CRDs at notebooks.odl.mit.edu/apps/{name}.

Published apps use the ol-marimo-app-client service account (client credentials
flow) for Trino access, so they are not tied to a specific user session.

The JupyterHub development environment (jupyter-data.odl.mit.edu) is managed
by the separate applications.jupyterhub_data stack.
"""

import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

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
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

marimo_data_config = Config("marimo_data")
vault_config = Config("vault")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

aws_config = AWSBase(
    tags={"OU": BusinessUnit.data, "Environment": stack_info.env_suffix}
)

marimo_namespace = "marimo"
marimo_operator_namespace = "marimo-operator-system"

k8s_global_labels = K8sGlobalLabels(
    service=Services.notebooks,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "marimo-data",
}

apps_domain = marimo_data_config.require("apps_domain")

# Vault policy for marimo published-app pods to read the service-account
# Trino client credentials (ol-marimo-app-client).
marimo_vault_policy_hcl = """
path "secret-operations/data/sso/marimo-app" {
  capabilities = ["read"]
}
path "secret-operations/sso/marimo-app" {
  capabilities = ["read"]
}
"""

marimo_vault_policy = vault.Policy(
    f"ol-marimo-data-vault-policy-{stack_info.env_suffix}",
    name="marimo-data",
    policy=marimo_vault_policy_hcl,
)

marimo_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"ol-marimo-data-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name="marimo-data",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[marimo_namespace],
    token_policies=[marimo_vault_policy.name],
)

marimo_vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name="marimo-data",
        namespace=marimo_namespace,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=marimo_vault_k8s_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[marimo_vault_k8s_auth_backend_role],
    ),
)

# VaultStaticSecret: syncs ol-marimo-app-client credentials for published apps
marimo_app_trino_secret = OLVaultK8SSecret(
    f"marimo-app-trino-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=k8s_global_labels,
        dest_secret_name="marimo-app-oidc-secret",  # pragma: allowlist secret  # noqa: E501, S106
        exclude_raw=True,
        excludes=[".*"],
        includes=["client_id", "client_secret"],
        labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v2",
        name="marimo-app-oidc-secret",
        namespace=marimo_namespace,
        path="sso/marimo-app",
        refresh_after="1h",
        templates={
            "MARIMO_APP_CLIENT_ID": '{{ get .Secrets "client_id" }}',
            "MARIMO_APP_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',  # pragma: allowlist secret  # noqa: E501
        },
        vaultauth=marimo_vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[marimo_vault_k8s_resources]),
)

# APISIX OIDC resources for the notebooks.odl.mit.edu published-apps gateway.
# Published apps require authentication; all authenticated ol-data-platform
# realm users can view any published app (per v1 scope decision).
marimo_oidc = OLApisixOIDCResources(
    f"ol-k8s-apisix-marimo-data-olapisixoidcresources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="marimo-data",
        k8s_labels=application_labels,
        k8s_namespace=marimo_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_scope="openid profile email",
        vault_mount="secret-operations",
        vault_path="sso/marimo",
        vaultauth=marimo_vault_k8s_resources.auth_name,
    ),
)

# TLS certificate for the published-apps domain
apps_tls_secret_name = "marimo-apps-tls-pair"  # noqa: S105  # pragma: allowlist secret
OLCertManagerCert(
    f"ol-marimo-data-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="marimo-data",
        k8s_namespace=marimo_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name=apps_tls_secret_name,
        dns_names=[apps_domain],
    ),
)

# Shared plugins for the published-apps APISIX route
marimo_shared_plugins = OLApisixSharedPlugins(
    name="ol-marimo-data-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="marimo-data",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=marimo_namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

# APISIX route for notebooks.odl.mit.edu — openid-connect plugin enforces auth,
# proxy-rewrite strips the /apps/ prefix before forwarding to the individual
# MarimoNotebook service. The actual per-notebook routing relies on the
# marimo-operator creating Services named after each MarimoNotebook resource.
OLApisixRoute(
    name=f"ol-marimo-data-k8s-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=marimo_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="marimo-data",
            priority=0,
            shared_plugin_config_name=marimo_shared_plugins.resource_name,
            plugins=[
                marimo_oidc.get_full_oidc_plugin_config(unauth_action="auth"),
            ],
            hosts=[apps_domain],
            paths=["/*"],
            backend_service_name="marimo-operator-gateway",
            backend_service_port=8080,
            websocket=True,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[marimo_oidc],
    ),
)
