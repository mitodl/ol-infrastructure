# ruff: noqa: E501
"""Deploy Opik (Comet's open-source LLM observability platform) on the data EKS cluster.

Opik provides tracing, evaluation, and prompt management for LLM applications.
This stack installs the upstream ``opik/opik`` Helm chart into the ``opik``
namespace and wires it to the pre-existing multi-tenant ClickHouse cluster that
is provisioned by the ``ol-application-clickhouse`` stack (database ``opik_db``,
user ``opik``).

Backing services:
- ClickHouse  — EXTERNAL. The bundled ClickHouse, the Altinity operator, and
  ZooKeeper that the chart would otherwise deploy are disabled; the chart's
  backend talks to ``clickhouse.clickhouse.svc.cluster.local`` instead.
- MySQL (state DB), Redis, and MinIO (object storage) — kept as the chart's
  bundled, namespace-local instances. These are internal-only (ClusterIP) and
  use the chart's default credentials, which is acceptable for the CI stack.

The ClickHouse ``opik`` user password lives in Vault at
``secret-clickhouse/credentials`` (key ``opik``). It is synced into a Kubernetes
Secret by the Vault Secrets Operator and injected into the backend Deployment via
``envFrom`` so that it reaches BOTH the ``backend-migrations`` init container and
the main backend container (the chart only wires its ``secretRefs`` into the main
container, not the migration init container) and never lands in a ConfigMap.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from bridge.lib.versions import OPIK_CHART_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrieval  ##
##################################

setup_vault_provider()

opik_config = Config("opik")
stack_info = parse_stack()

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))

OPIK_NAMESPACE = "opik"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(OPIK_NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.data,
        "Environment": f"data-{stack_info.env_suffix}",
        "Application": "opik",
        "Owner": "platform-engineering",
    }
)

# Typed labels for OL component resources (e.g. the Vault auth binding).
k8s_labels = K8sGlobalLabels(
    ou=BusinessUnit.data,
    service=Services.opik,
    stack=stack_info,
)

# Plain label dict applied to the raw K8s objects we manage directly.
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.k8s_name,
    "ol.mit.edu/stack": stack_info.k8s_name,
    "app.kubernetes.io/managed-by": "pulumi",
}

# External ClickHouse connection details (see the ol-application-clickhouse stack).
CLICKHOUSE_HOST = "clickhouse.clickhouse.svc.cluster.local"
CLICKHOUSE_HTTP_PORT = 8123
CLICKHOUSE_DATABASE = "opik_db"
CLICKHOUSE_USER = "opik"

# Vault KV-v2 mount + path holding the ClickHouse credentials. The ``opik`` key
# is the password for the ``opik`` ClickHouse user / ``opik_db`` database.
CLICKHOUSE_VAULT_MOUNT = "secret-clickhouse"
CLICKHOUSE_VAULT_PATH = "credentials"

opik_domain = (
    opik_config.get("k8s_domain") or f"opik-{stack_info.env_suffix}.ol.mit.edu"
)

################################################
##  IRSA + Vault Auth Binding (for VSO sync)  ##
################################################
# No AWS access is required (object storage is the chart's bundled MinIO), so
# no IAM policy is attached. The binding's purpose here is to provision the
# Vault Secrets Operator wiring (VaultConnection / VaultAuth / sync service
# account) plus a Vault policy granting read access to the ClickHouse KV mount.
opik_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="opik",
        namespace=OPIK_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("opik_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="opik-backend",
        vault_sync_service_account_names="opik-vault",
        k8s_labels=k8s_labels,
    )
)

############################################################
# VSO K8s Secret — ClickHouse credentials as env vars
#
# The Vault Secrets Operator renders the templates below into a K8s Secret. Raw
# Vault keys (admin, tensorzero, openlit, ...) are excluded by default; only the
# rendered env-var keys are written. Both keys map to the single ``opik`` user
# password so the backend can use it for runtime queries and migrations alike.
############################################################
clickhouse_credentials_secret_name = (
    "opik-clickhouse-credentials"  # pragma: allowlist secret  # noqa: S105
)
clickhouse_credentials_secret = OLVaultK8SSecret(
    f"opik-clickhouse-credentials-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="opik-clickhouse-credentials",
        namespace=OPIK_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=clickhouse_credentials_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=CLICKHOUSE_VAULT_MOUNT,
        mount_type="kv-v2",
        path=CLICKHOUSE_VAULT_PATH,
        templates={
            "ANALYTICS_DB_PASS": '{{- get .Secrets "opik" -}}',
            "ANALYTICS_DB_MIGRATIONS_PASS": '{{- get .Secrets "opik" -}}',
        },
        refresh_after="1h",
        vaultauth=opik_app.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=opik_app.vault_k8s_resources,
    ),
)

###################################
#   Opik Helm Release (K8S)       #
###################################
opik_helm_release = kubernetes.helm.v3.Release(
    f"opik-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="opik",
        chart="opik",
        version=OPIK_CHART_VERSION,
        namespace=OPIK_NAMESPACE,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://comet-ml.github.io/opik/",
        ),
        values={
            # Use the shared data-cluster ClickHouse rather than the bundled one.
            "clickhouse": {"enabled": False},
            "altinity-clickhouse-operator": {"enabled": False},
            "zookeeper": {"enabled": False},
            # Fresh install — there is nothing to migrate from the legacy
            # Bitnami-based subcharts.
            "chartMigration": {"enabled": False},
            "component": {
                "backend": {
                    # Gate backend startup on the external ClickHouse being
                    # reachable over HTTP.
                    "waitForClickhouse": {
                        "clickhouse": {
                            "host": CLICKHOUSE_HOST,
                            "port": CLICKHOUSE_HTTP_PORT,
                            "protocol": "http",
                        },
                    },
                    # Non-secret ClickHouse connection settings (these land in
                    # the opik-backend ConfigMap). Passwords are injected via
                    # the secretRef in envFrom below.
                    "env": {
                        # Database-less URL on purpose: opik's Liquibase
                        # migrations hard-code the changelog bookkeeping tables
                        # (DATABASECHANGELOG / DATABASECHANGELOGLOCK, and the
                        # ``...1`` shadow tables created by
                        # 000017_change_tables_to_replicated) into the ``default``
                        # database; only the application tables honor
                        # ANALYTICS_DB_DATABASE_NAME. The connection's default
                        # catalog must therefore stay ``default`` so the live
                        # changelog matches what those changesets manipulate. The
                        # ``opik`` ClickHouse user is granted access to ``default``
                        # for exactly this (see ol-application-clickhouse).
                        "ANALYTICS_DB_MIGRATIONS_URL": f"jdbc:clickhouse://{CLICKHOUSE_HOST}:{CLICKHOUSE_HTTP_PORT}",
                        "ANALYTICS_DB_PROTOCOL": "HTTP",
                        "ANALYTICS_DB_HOST": CLICKHOUSE_HOST,
                        "ANALYTICS_DB_PORT": str(CLICKHOUSE_HTTP_PORT),
                        "ANALYTICS_DB_DATABASE_NAME": CLICKHOUSE_DATABASE,
                        "ANALYTICS_DB_USERNAME": CLICKHOUSE_USER,
                        "ANALYTICS_DB_MIGRATIONS_USER": CLICKHOUSE_USER,
                    },
                    # Layer the VSO-synced credentials Secret on top of the
                    # ConfigMap. Listing the Secret last lets it override the
                    # placeholder password values, and because the migration
                    # init container also consumes ``envFrom`` it gets the real
                    # password too.
                    "envFrom": [
                        {"configMapRef": {"name": "opik-backend"}},
                        {"secretRef": {"name": clickhouse_credentials_secret_name}},
                    ],
                },
                # The frontend is exposed via the Gateway API resources below
                # rather than the chart's own ingress.
                "frontend": {
                    "ingress": {"enabled": False},
                },
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[clickhouse_credentials_secret],
    ),
)

########################################
# Gateway API routing + TLS certificate #
########################################
opik_gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="opik",
    namespace=OPIK_NAMESPACE,
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https",
            hostname=opik_domain,
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="opik-tls",  # pragma: allowlist secret  # noqa: S106
            certificate_secret_namespace=OPIK_NAMESPACE,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="opik-frontend",
            backend_service_namespace=OPIK_NAMESPACE,
            backend_service_port=5173,
            name="opik-https-root",
            listener_name="https",
            hostnames=[opik_domain],
            port=8443,
            matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
        ),
    ],
)

opik_gateway = OLEKSGateway(
    "opik-gateway",
    gateway_config=opik_gateway_config,
    opts=ResourceOptions(parent=opik_helm_release, depends_on=[opik_helm_release]),
)

export("opik_namespace", OPIK_NAMESPACE)
export("opik_url", f"https://{opik_domain}")
