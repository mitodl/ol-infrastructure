"""Deploy the ToolHive operator on the operations EKS cluster.

ToolHive (https://docs.stacklok.com/toolhive) is Stacklok's Kubernetes operator
for declaratively running MCP (Model Context Protocol) servers. This stack installs
two Helm charts published as OCI artifacts under
``oci://ghcr.io/stacklok/toolhive``:

- ``toolhive-operator-crds`` — the ``MCPServer`` (and related) CRDs.
- ``toolhive-operator`` — the operator that reconciles ``MCPServer`` resources
  into running proxy Deployments + Services.

It also creates a single sample ``MCPServer`` (the ToolHive docs search server) to
validate the install end-to-end. The operator runs cluster-scoped (the chart default),
so it can manage ``MCPServer`` resources in any namespace. The ``toolhive`` namespace
is pre-created by the ``ol-infrastructure-eks`` ``operations`` stack and validated here
via ``check_cluster_namespace``.

This is the initial CI-only deployment: no external ingress (APISIX/Gateway), TLS, or
Vault/IRSA wiring is configured yet. The sample server is reachable only in-cluster.
"""

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions, export

from bridge.lib.versions import (
    TOOLHIVE_OPERATOR_CHART_VERSION,
    TOOLHIVE_OPERATOR_CRDS_CHART_VERSION,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)

stack_info = parse_stack()

# K8s stack reference + provider for the operations cluster.
cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

TOOLHIVE_NAMESPACE = "toolhive"

# The namespace is provisioned by the EKS operations stack; fail fast if missing.
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(TOOLHIVE_NAMESPACE, ns)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.toolhive,
    ou=BusinessUnit.operations,
    stack=stack_info,
).model_dump()

###################################
#   ToolHive operator CRDs        #
###################################
# Installed as a separate release so the CRDs exist before the operator (and the
# sample MCPServer) are reconciled. CRDs are cluster-scoped; the namespace only
# anchors the Helm release bookkeeping.
toolhive_crds_release = kubernetes.helm.v3.Release(
    f"toolhive-operator-crds-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="toolhive-operator-crds",
        chart="oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds",
        version=TOOLHIVE_OPERATOR_CRDS_CHART_VERSION,
        namespace=TOOLHIVE_NAMESPACE,
        cleanup_on_fail=True,
        skip_await=False,
        values={},
    ),
)

###################################
#   ToolHive operator             #
###################################
toolhive_operator_release = kubernetes.helm.v3.Release(
    f"toolhive-operator-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="toolhive-operator",
        chart="oci://ghcr.io/stacklok/toolhive/toolhive-operator",
        version=TOOLHIVE_OPERATOR_CHART_VERSION,
        namespace=TOOLHIVE_NAMESPACE,
        cleanup_on_fail=True,
        skip_await=False,
        # CRDs are managed by the dedicated release above.
        skip_crds=True,
        values={
            "operator": {
                # Cluster-scoped: reconcile MCPServer resources in any namespace.
                "rbac": {"scope": "cluster"},
            },
        },
    ),
    opts=ResourceOptions(depends_on=[toolhive_crds_release]),
)

#########################################
#   Sample MCPServer (validation only)  #
#########################################
# The ToolHive docs search server. The operator reconciles this into a proxy
# Deployment + Service (``mcp-toolhive-docs-proxy``) reachable in-cluster at
# ``http://mcp-toolhive-docs-proxy.toolhive.svc.cluster.local:8080/mcp``.
toolhive_sample_mcpserver = kubernetes.apiextensions.CustomResource(
    f"toolhive-sample-mcpserver-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="toolhive-docs",
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "image": "ghcr.io/stackloklabs/toolhive-doc-mcp",
        "transport": "streamable-http",
        "proxyPort": 8080,
        "mcpPort": 8080,
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
    },
    opts=ResourceOptions(depends_on=[toolhive_operator_release]),
)

export("toolhive_namespace", TOOLHIVE_NAMESPACE)
