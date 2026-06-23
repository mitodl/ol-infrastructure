"""Deploy OpenLIT (LLM/GenAI observability) onto the data EKS cluster.

OpenLIT is installed from its official Helm chart
(https://docs.openlit.io/latest/openlit/installation#kubernetes).  The chart can
bundle its own ClickHouse, but here we DISABLE that and point OpenLIT at the
shared multi-tenant ClickHouse cluster managed by the
``applications.clickhouse`` stack, which already provisions an ``openlit`` user,
the ``openlit_db`` database, an LLMOps quota, and a NetworkPolicy permitting the
``openlit`` namespace to reach ClickHouse.

The ``openlit`` ClickHouse password lives in Vault KV at
``secret-clickhouse/credentials`` (key ``openlit``).  It is synced into a K8s
secret in the ``openlit`` namespace via the vault-secrets-operator and consumed
by the chart through ``config.secret``.

NOTE: the ``openlit_db`` database must be created manually on the ClickHouse
cluster before OpenLIT can persist telemetry (there is no ClickHouse Pulumi
provider).  See ``applications/clickhouse/__main__.py``.

The platform UI listens on port 3000 and is exposed via the cluster's Traefik
Gateway API (OLEKSGateway) with a cert-manager issued TLS certificate.
"""

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions

from bridge.lib.versions import OPENLIT_CHART_VERSION
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

stack_info = parse_stack()
setup_vault_provider(stack_info)

openlit_config = Config("openlit")
vault_config = Config("vault")

# Setup K8S provider for the data cluster deployment
cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

###################################
#   Kubernetes Deployment (K8S)   #
###################################

openlit_namespace = "openlit"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(openlit_namespace, ns)
)

k8s_global_labels = K8sAppLabels(
    application=Application.openlit,
    service=Services.openlit,
    product=Product.data,
    ou=BusinessUnit.data,
    source_repository="https://github.com/openlit/openlit",
    stack=stack_info,
).model_dump()

openlit_domain = openlit_config.require("domain")

# OpenLIT platform UI service name and port, sourced from the helm chart
# (service name == release name, port defaults to 3000).
openlit_service_name = "openlit"
openlit_service_port = 3000

##########################################################
#   Vault: sync the shared ClickHouse `openlit` creds    #
##########################################################
# The shared ClickHouse cluster stores per-tool credentials in Vault KV at
# secret-clickhouse/credentials. Grant the openlit namespace read access and
# sync the openlit user's password into a K8s secret for the chart to consume.
openlit_vault_policy = vault.Policy(
    f"openlit-vault-policy-{stack_info.env_suffix}",
    name="openlit",
    policy="""
path "secret-clickhouse/data/credentials" {
  capabilities = ["read"]
}
path "secret-clickhouse/credentials" {
  capabilities = ["read"]
}
""",
)

openlit_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"openlit-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name="openlit",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[openlit_namespace],
    token_policies=[openlit_vault_policy.name],
)

openlit_vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name="openlit",
        namespace=openlit_namespace,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=openlit_vault_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[openlit_vault_auth_backend_role],
    ),
)

# K8s secret consumed by the chart via config.secret. The chart expects a
# username key and a password key; the username is the static `openlit`
# ClickHouse user and the password is sourced from Vault.
clickhouse_creds_secret_name = "openlit-clickhouse-creds"  # noqa: S105  # pragma: allowlist secret
clickhouse_creds_secret = OLVaultK8SSecret(
    f"openlit-clickhouse-creds-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="openlit-clickhouse-creds",
        namespace=openlit_namespace,
        dest_secret_name=clickhouse_creds_secret_name,
        dest_secret_labels=k8s_global_labels,
        labels=k8s_global_labels,
        mount="secret-clickhouse",
        mount_type="kv-v2",
        path="credentials",
        templates={
            "username": "openlit",
            "password": '{{ get .Secrets "openlit" }}',
        },
        vaultauth=openlit_vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(depends_on=[openlit_vault_k8s_resources]),
)

# ClickHouse connection details for the shared cluster managed by the
# applications.clickhouse stack (stable ClusterIP service `clickhouse` in the
# `clickhouse` namespace). The `openlit_db` database must already exist.
clickhouse_host = (
    openlit_config.get("clickhouse_host") or "clickhouse.clickhouse.svc.cluster.local"
)
clickhouse_port = openlit_config.get("clickhouse_port") or "8123"
clickhouse_database = openlit_config.get("clickhouse_database") or "openlit_db"

# Deploy OpenLIT using the official Helm chart.
# Ref: https://docs.openlit.io/latest/openlit/installation#kubernetes
openlit_helm_release = kubernetes.helm.v3.Release(
    f"openlit-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="openlit",
        chart="openlit",
        version=OPENLIT_CHART_VERSION,
        namespace=openlit_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://openlit.github.io/helm/",
        ),
        values={
            "additionalLabels": k8s_global_labels,
            "replicaCount": 1,
            # Expose the platform UI as a ClusterIP service; external access is
            # handled by the Traefik Gateway below rather than a LoadBalancer.
            "service": {
                "type": "ClusterIP",
                "port": openlit_service_port,
            },
            # Use the shared multi-tenant ClickHouse cluster instead of the
            # bundled one.
            "clickhouse": {
                "enabled": False,
            },
            "config": {
                "database": {
                    "host": clickhouse_host,
                    "port": clickhouse_port,
                    "name": clickhouse_database,
                },
                # Pull the ClickHouse username/password from the VSO-synced
                # secret rather than embedding them in the release values.
                "secret": {  # pragma: allowlist secret
                    "name": clickhouse_creds_secret_name,
                    "usernameKey": "username",
                    "passwordKey": "password",  # pragma: allowlist secret
                },
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[clickhouse_creds_secret],
    ),
)

# Expose the OpenLIT UI via the cluster's Traefik Gateway API. cert-manager
# issues the TLS certificate referenced by the HTTPS listener.
openlit_gateway = OLEKSGateway(
    f"openlit-{stack_info.env_suffix}-gateway",
    gateway_config=OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name="openlit",
        labels=k8s_global_labels,
        namespace=openlit_namespace,
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https",
                hostname=openlit_domain,
                port=8443,
                tls_mode="Terminate",
                certificate_secret_name="openlit-tls",  # noqa: S106  # pragma: allowlist secret
                certificate_secret_namespace=openlit_namespace,
            ),
        ],
        routes=[
            OLEKSGatewayRouteConfig(
                backend_service_name=openlit_service_name,
                backend_service_namespace=openlit_namespace,
                backend_service_port=openlit_service_port,
                hostnames=[openlit_domain],
                name="openlit-https",
                listener_name="https",
                port=8443,
            ),
        ],
    ),
    opts=ResourceOptions(
        parent=openlit_helm_release,
        delete_before_replace=True,
        depends_on=[openlit_helm_release],
    ),
)
