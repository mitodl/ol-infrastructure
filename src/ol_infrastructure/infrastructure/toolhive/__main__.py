"""ToolHive MCP platform infrastructure — Operator, Registry Server, vMCP Gateway.

Stack naming: infrastructure.toolhive.<purpose>.<env>
  e.g., infrastructure.toolhive.developers.Production
        infrastructure.toolhive.appmcps.Production
        infrastructure.toolhive.datamcps.Production

Each ToolHive installation targets a specific EKS cluster and use case:
  - "developers":  Operations cluster. OIDC-authenticated via Keycloak
                   ol-platform-engineering realm. Developer tools (Claude,
                   Copilot, Continue) connect here.
  - "appmcps":     Applications cluster. Open gateway for agentic workloads
                   serving application-tier MCPs.
  - "datamcps":    Data cluster. Open gateway for agentic workloads
                   serving data-tier MCPs.

Components per installation (all namespaced; CRDs are cluster-scoped):
  - ToolHive Operator CRDs  (MCPServer custom resource — cluster-scoped, idempotent)
  - ToolHive Operator        (reconciles MCPServer → Deployment + Service, ns-scoped)
  - ToolHive Registry Server (discovers MCPServer resources in this namespace)
  - vMCP Gateway             (single /mcp endpoint aggregating registered MCPServers)

OIDC (developers installation only):
  Keycloak realm:  ol-platform-engineering
  Client ID:       ol-toolhive-developers-client
  Issuer URL:      https://sso{-qa}.ol.mit.edu/realms/ol-platform-engineering
  Client secret:   toolhive:oidc_client_secret (SOPS-encrypted in stack YAML)

Required stack config:
  toolhive:eks_cluster   — EKS cluster name: "operations", "applications", or "data"
  toolhive:enable_oidc   — "true" for developer installations, "false" for agent ones
"""

import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, StackReference, export

from bridge.lib.versions import TOOLHIVE_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack, require_stack_output_value

stack_info = parse_stack()
toolhive_config = Config("toolhive")

# stack_info.env_prefix = purpose (e.g., "developers", "appmcps", "datamcps")
# stack_info.env_suffix = environment (e.g., "production")
# stack_info.name       = environment component (e.g., "Production") — StackRef names
purpose = stack_info.env_prefix
env = stack_info.env_suffix

eks_cluster: str = toolhive_config.require("eks_cluster")

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{eks_cluster}.{stack_info.name}"
)
setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))

aws_config = AWSBase(tags={"OU": "operations", "Environment": env})

k8s_global_labels: dict[str, str] = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/managed-by": "pulumi",
    "ol.mit.edu/service": "toolhive",
    "ol.mit.edu/purpose": purpose,
}

# Each installation lives in its own namespace to keep developer and agent
# MCPServer resources isolated from each other.
TOOLHIVE_NAMESPACE = f"toolhive-{purpose}"

enable_oidc: bool = toolhive_config.get_bool("enable_oidc") or False
oidc_issuer_url: str = toolhive_config.get("oidc_issuer_url") or ""
oidc_client_id: str = toolhive_config.get("oidc_client_id") or ""
oidc_client_secret: Output[str] | None = (
    toolhive_config.get_secret("oidc_client_secret") if enable_oidc else None
)
registry_replicas: int = toolhive_config.get_int("registry_replicas") or 1

toolhive_namespace = kubernetes.core.v1.Namespace(
    f"toolhive-{purpose}-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=TOOLHIVE_NAMESPACE,
        labels={
            **k8s_global_labels,
            "kubernetes.io/metadata.name": TOOLHIVE_NAMESPACE,
        },
    ),
)

# ToolHive Operator CRDs — registers the MCPServer CRD cluster-wide.
# These are cluster-scoped resources; deploying from multiple stacks is safe
# because Helm treats existing CRDs as already-satisfied.
toolhive_crds = kubernetes.helm.v3.Release(
    f"toolhive-{purpose}-operator-crds",
    kubernetes.helm.v3.ReleaseArgs(
        name=f"toolhive-{purpose}-operator-crds",
        chart="oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds",
        version=TOOLHIVE_OPERATOR_CHART_VERSION,
        namespace=TOOLHIVE_NAMESPACE,
        cleanup_on_fail=True,
        values={"global": {"labels": k8s_global_labels}},
    ),
    opts=ResourceOptions(
        parent=toolhive_namespace,
        depends_on=[toolhive_namespace],
        delete_before_replace=True,
    ),
)

# ToolHive Operator — scoped to this installation's namespace so developer and
# agent MCPServer resources are managed by independent operator instances.
toolhive_operator = kubernetes.helm.v3.Release(
    f"toolhive-{purpose}-operator",
    kubernetes.helm.v3.ReleaseArgs(
        name=f"toolhive-{purpose}-operator",
        chart="oci://ghcr.io/stacklok/toolhive/toolhive-operator",
        version=TOOLHIVE_OPERATOR_CHART_VERSION,
        namespace=TOOLHIVE_NAMESPACE,
        cleanup_on_fail=True,
        values={
            "global": {"labels": k8s_global_labels},
            "operator": {
                "replicas": 1,
                "watchNamespace": TOOLHIVE_NAMESPACE,
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"memory": "256Mi"},
                },
            },
        },
    ),
    opts=ResourceOptions(
        parent=toolhive_namespace,
        depends_on=[toolhive_namespace, toolhive_crds],
        delete_before_replace=True,
    ),
)

# OIDC Secret — only created for OIDC-enabled (developers) installations.
# Holds the Keycloak client credentials injected into Registry and vMCP Gateway.
oidc_secret: kubernetes.core.v1.Secret | None = None
if enable_oidc and oidc_client_secret is not None:
    oidc_secret = kubernetes.core.v1.Secret(
        f"toolhive-{purpose}-oidc-secret",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"toolhive-{purpose}-oidc",
            namespace=TOOLHIVE_NAMESPACE,
            labels=k8s_global_labels,
        ),
        string_data={
            "client_id": oidc_client_id,
            "client_secret": oidc_client_secret,
        },
        opts=ResourceOptions(
            parent=toolhive_namespace,
            depends_on=[toolhive_namespace],
            delete_before_replace=True,
        ),
    )

# Build OIDC-aware args + env for Registry Server and vMCP Gateway.
registry_args: list[str] = [
    "--source-type=kubernetes",
    f"--namespace={TOOLHIVE_NAMESPACE}",
]
registry_env: list[kubernetes.core.v1.EnvVarArgs] = []

vmcp_args: list[str] = [
    f"--registry-url=http://toolhive-{purpose}-registry"
    f".{TOOLHIVE_NAMESPACE}.svc.cluster.local:8080",
]
vmcp_env: list[kubernetes.core.v1.EnvVarArgs] = []

if enable_oidc:
    # ToolHive uses TOOLHIVE_AUTH_OIDC_* env vars for OIDC configuration.
    # Issuer URL and client ID are non-sensitive and stored in stack config.
    # The client secret is SOPS-encrypted in the stack YAML and injected via K8s Secret.
    oidc_env: list[kubernetes.core.v1.EnvVarArgs] = [
        kubernetes.core.v1.EnvVarArgs(
            name="TOOLHIVE_AUTH_OIDC_ISSUER_URL",
            value=oidc_issuer_url,
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="TOOLHIVE_AUTH_OIDC_CLIENT_ID",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=f"toolhive-{purpose}-oidc",
                    key="client_id",
                )
            ),
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="TOOLHIVE_AUTH_OIDC_CLIENT_SECRET",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=f"toolhive-{purpose}-oidc",
                    key="client_secret",
                )
            ),
        ),
    ]
    registry_env.extend(oidc_env)
    vmcp_env.extend(oidc_env)

oidc_secret_dep = [oidc_secret] if oidc_secret else []

# ToolHive Registry Server — discovers MCPServer resources in this namespace
# and powers the catalog that the vMCP Gateway aggregates.
registry_server = kubernetes.apps.v1.Deployment(
    f"toolhive-{purpose}-registry-server",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"toolhive-{purpose}-registry-server",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": f"toolhive-{purpose}-registry"},
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=registry_replicas,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": f"toolhive-{purpose}-registry"},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={**k8s_global_labels, "app": f"toolhive-{purpose}-registry"},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=f"toolhive-{purpose}-registry",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="registry",
                        image="ghcr.io/stacklok/toolhive/registry-server:latest",
                        args=registry_args,
                        env=registry_env,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="http", container_port=8080
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "50m", "memory": "64Mi"},
                            limits={"memory": "128Mi"},
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        parent=toolhive_namespace,
        depends_on=[toolhive_operator, *oidc_secret_dep],
        delete_before_replace=True,
    ),
)

kubernetes.core.v1.Service(
    f"toolhive-{purpose}-registry-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"toolhive-{purpose}-registry",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": f"toolhive-{purpose}-registry"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": f"toolhive-{purpose}-registry"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(name="http", port=8080, target_port=8080)
        ],
    ),
    opts=ResourceOptions(parent=registry_server),
)

# vMCP Gateway — single /mcp endpoint aggregating all MCPServer resources.
# Developer installations require an OIDC token; agent installations are open
# (access controlled at the network-policy / service-mesh layer).
vmcp_gateway = kubernetes.apps.v1.Deployment(
    f"toolhive-{purpose}-vmcp-gateway",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"toolhive-{purpose}-vmcp-gateway",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": f"toolhive-{purpose}-vmcp"},
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": f"toolhive-{purpose}-vmcp"},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={**k8s_global_labels, "app": f"toolhive-{purpose}-vmcp"},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=f"toolhive-{purpose}-vmcp",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="vmcp",
                        image="ghcr.io/stacklok/toolhive/vmcp:latest",
                        args=vmcp_args,
                        env=vmcp_env,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="mcp", container_port=9090
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "50m", "memory": "64Mi"},
                            limits={"memory": "128Mi"},
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        parent=toolhive_namespace,
        depends_on=[registry_server, *oidc_secret_dep],
        delete_before_replace=True,
    ),
)

kubernetes.core.v1.Service(
    f"toolhive-{purpose}-vmcp-gateway-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"toolhive-{purpose}-vmcp",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": f"toolhive-{purpose}-vmcp"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": f"toolhive-{purpose}-vmcp"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(name="mcp", port=9090, target_port=9090)
        ],
    ),
    opts=ResourceOptions(parent=vmcp_gateway),
)

registry_svc_url = (
    f"http://toolhive-{purpose}-registry.{TOOLHIVE_NAMESPACE}.svc.cluster.local:8080"
)
vmcp_gateway_url = (
    f"http://toolhive-{purpose}-vmcp.{TOOLHIVE_NAMESPACE}.svc.cluster.local:9090/mcp"
)

export("operator_namespace", TOOLHIVE_NAMESPACE)
export("eks_cluster", eks_cluster)
export("purpose", purpose)
export("oidc_enabled", str(enable_oidc).lower())
export("registry_url", registry_svc_url)
export("vmcp_gateway_url", vmcp_gateway_url)
