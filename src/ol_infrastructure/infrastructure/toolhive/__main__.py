"""ToolHive MCP platform infrastructure — Operator, Registry Server, vMCP Gateway.

Stack naming: infrastructure.toolhive.Production

This stack installs the ToolHive Kubernetes ecosystem:
- ToolHive Operator CRDs (enables MCPServer custom resource)
- ToolHive Operator (reconciles MCPServer → Deployment + Service)
- ToolHive Registry Server (auto-discovers MCPServer resources in cluster)
- vMCP Gateway (single /mcp endpoint aggregating all registered MCPServer resources)

After this stack is deployed, application stacks create MCPServer CRD resources
and the operator automatically provisions and registers them.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from bridge.lib.versions import TOOLHIVE_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
toolhive_config = Config("toolhive")

aws_config = AWSBase(tags={"OU": "operations", "Environment": stack_info.env_suffix})

k8s_global_labels: dict[str, str] = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/managed-by": "pulumi",
    "ol.mit.edu/service": "toolhive",
}

TOOLHIVE_NAMESPACE = "toolhive-system"

toolhive_namespace = kubernetes.core.v1.Namespace(
    "toolhive-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=TOOLHIVE_NAMESPACE,
        labels={
            **k8s_global_labels,
            "kubernetes.io/metadata.name": TOOLHIVE_NAMESPACE,
        },
    ),
)

# ToolHive Operator CRDs — installs the MCPServer custom resource definition.
# Must be installed before the operator so the CRD exists when the operator starts.
toolhive_crds = kubernetes.helm.v3.Release(
    "toolhive-operator-crds",
    kubernetes.helm.v3.ReleaseArgs(
        name="toolhive-operator-crds",
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

# ToolHive Operator — watches MCPServer CRs and creates Deployments + Services.
toolhive_operator = kubernetes.helm.v3.Release(
    "toolhive-operator",
    kubernetes.helm.v3.ReleaseArgs(
        name="toolhive-operator",
        chart="oci://ghcr.io/stacklok/toolhive/toolhive-operator",
        version=TOOLHIVE_OPERATOR_CHART_VERSION,
        namespace=TOOLHIVE_NAMESPACE,
        cleanup_on_fail=True,
        values={
            "global": {"labels": k8s_global_labels},
            "operator": {
                "replicas": 1,
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

# ToolHive Registry Server — auto-discovers MCPServer resources in the cluster
# and exposes a registry API + web portal for browsing available MCP servers.
registry_replicas = toolhive_config.get_int("registry_replicas") or 1
registry_server = kubernetes.apps.v1.Deployment(
    "toolhive-registry-server",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="toolhive-registry-server",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": "toolhive-registry"},
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=registry_replicas,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "toolhive-registry"},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={**k8s_global_labels, "app": "toolhive-registry"},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name="toolhive-registry",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="registry",
                        image="ghcr.io/stacklok/toolhive/registry-server:latest",
                        args=[
                            "--source-type=kubernetes",
                            f"--namespace={TOOLHIVE_NAMESPACE}",
                        ],
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
        depends_on=[toolhive_operator],
        delete_before_replace=True,
    ),
)

registry_service = kubernetes.core.v1.Service(
    "toolhive-registry-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="toolhive-registry",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": "toolhive-registry"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "toolhive-registry"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(name="http", port=8080, target_port=8080)
        ],
    ),
    opts=ResourceOptions(parent=registry_server),
)

# vMCP Gateway — single unified /mcp endpoint that aggregates all MCPServer resources.
# Developer tools connect to this single endpoint instead of per-server endpoints.
vmcp_gateway = kubernetes.apps.v1.Deployment(
    "toolhive-vmcp-gateway",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="toolhive-vmcp-gateway",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": "toolhive-vmcp"},
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "toolhive-vmcp"},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={**k8s_global_labels, "app": "toolhive-vmcp"},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name="toolhive-vmcp",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="vmcp",
                        image="ghcr.io/stacklok/toolhive/vmcp:latest",
                        args=[
                            f"--registry-url=http://toolhive-registry.{TOOLHIVE_NAMESPACE}.svc.cluster.local:8080",
                        ],
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
        depends_on=[registry_server],
        delete_before_replace=True,
    ),
)

vmcp_gateway_service = kubernetes.core.v1.Service(
    "toolhive-vmcp-gateway-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="toolhive-vmcp",
        namespace=TOOLHIVE_NAMESPACE,
        labels={**k8s_global_labels, "app": "toolhive-vmcp"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "toolhive-vmcp"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(name="mcp", port=9090, target_port=9090)
        ],
    ),
    opts=ResourceOptions(parent=vmcp_gateway),
)

export("operator_namespace", TOOLHIVE_NAMESPACE)
export(
    "registry_url",
    f"http://toolhive-registry.{TOOLHIVE_NAMESPACE}.svc.cluster.local:8080",
)
export(
    "vmcp_gateway_url",
    f"http://toolhive-vmcp.{TOOLHIVE_NAMESPACE}.svc.cluster.local:9090/mcp",
)
