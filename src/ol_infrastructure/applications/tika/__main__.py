"""Create the resources needed to run a tika server.  # noqa: D200"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import TIKA_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    BusinessUnit,
    K8sGlobalLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

stack_info = parse_stack()
tika_config = Config("tika")

# Setup K8S provider for Kubernetes deployment
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Load X-Access-Token from secrets
x_access_token = read_yaml_secrets(Path(f"tika/tika.{stack_info.env_suffix}.yaml"))[
    "x_access_token"
]

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

# Store the access token in vault
vault.generic.Secret(
    "tika-server-x-access-token-vault-secret",
    path="secret-operations/tika/access-token",
    data_json=json.dumps({"value": x_access_token}),
)

###################################
#   Kubernetes Deployment (K8S)   #
###################################

# Setup K8s namespace
tika_namespace = "tika"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(tika_namespace, ns)
)

# K8s labels for Tika
k8s_global_labels = K8sGlobalLabels(
    application=Application.tika,
    service=Services.tika,
    product=Product.data,
    ou=BusinessUnit.operations,
    source_repository="https://apache.jfrog.io/artifactory/tika",
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "tika",
}

# Get K8s domain from config
tika_k8s_domain = tika_config.require("k8s_domain")

# Create TLS certificate for K8s deployment
tls_secret_name = "tika-tls-pair"  # pragma: allowlist secret  # noqa: S105
tika_cert = OLCertManagerCert(
    f"ol-tika-tls-cert-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="tika",
        k8s_namespace=tika_namespace,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        apisixtls_ingress_class=tika_config.get("apisix_ingress_class")
        or "apache-apisix",
        dest_secret_name=tls_secret_name,
        dns_names=[
            tika_k8s_domain,
        ],
    ),
)

# Create Kubernetes secret for X-Access-Token
tika_access_token_secret = kubernetes.core.v1.Secret(
    f"tika-access-token-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="tika-access-token",
        namespace=tika_namespace,
        labels=application_labels,
    ),
    string_data={
        "x-access-token": x_access_token,
    },
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Deploy Tika using Helm chart
tika_helm_release = kubernetes.helm.v3.Release(
    f"tika-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="tika",
        chart="tika",
        version=TIKA_CHART_VERSION,
        namespace=tika_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://apache.jfrog.io/artifactory/tika",
        ),
        values={
            "commonLabels": k8s_global_labels,
            "replicaCount": 2,
            "service": {
                "type": "ClusterIP",
                "port": 9998,
            },
            "resources": {
                "requests": {
                    "cpu": "10m",  # does nothing most of the time
                    "memory": "2Gi",  # java never gives it back
                },
                "limits": {
                    "memory": "2Gi",
                },
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Create shared APISIX plugins for Tika
tika_shared_plugins = OLApisixSharedPlugins(
    name=f"tika-apisix-shared-plugins-{stack_info.env_suffix}",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="tika",
        resource_suffix="shared-plugins",
        k8s_namespace=tika_namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

# Create APISIX route with header token authentication
# Using serverless-pre-function to validate the X-Access-Token header
tika_apisix_route = OLApisixRoute(
    name=f"tika-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=tika_namespace,
    k8s_labels=application_labels,
    ingress_class_name=tika_config.get("apisix_ingress_class") or "apache-apisix",
    route_configs=[
        OLApisixRouteConfig(
            route_name="tika-all",
            priority=10,
            shared_plugin_config_name=tika_shared_plugins.resource_name,
            timeout_send="300s",  # Tika can take a while to process large files
            plugins=[
                # Use serverless-pre-function to validate X-Access-Token header
                # This is simpler than request-validation and more flexible
                OLApisixPluginConfig(
                    name="serverless-pre-function",
                    config={
                        "phase": "access",
                        "functions": [
                            f"""
return function(conf, ctx)
    local core = require("apisix.core")
    local token = core.request.header(ctx, "X-Access-Token")
    local expected = "{x_access_token}"
    if not token or token ~= expected then
        return 401, {{error = "Unauthorized"}}
    end
end
                            """.strip()
                        ],
                    },
                ),
            ],
            hosts=[tika_k8s_domain],
            paths=["/*"],
            backend_service_name="tika",
            backend_service_port=9998,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[tika_helm_release, tika_access_token_secret],
    ),
)
