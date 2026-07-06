"""Deploy the data agent-class MCP workloads on the operations cluster.

This stack owns the ``toolhive-data`` namespace: the ``MCPServer`` resources (and,
in a later iteration, ``VirtualMCPServer`` / ``MCPGroup`` resources) consumed by
data agents. The ToolHive operator and CRDs that reconcile these resources are
installed cluster-scoped by the ``ol-application-toolhive-operator`` stack; this
stack references that one so it fails fast if the operator has never been deployed.

See ``../toolhive_operator/DEPLOYMENT_STRATEGY.md`` for why agent classes are
separated by namespace under a single operator, and when a data-agent MCP server
that needs IRSA into the data account should instead graduate to the ``data``
cluster.

This is the initial CI-only bootstrap: it establishes the namespace ownership and
operator dependency. The MCPServer / VirtualMCPServer / MCPGroup workloads for this
class are added in a follow-up.
"""

from pulumi import export

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
    require_stack_output_value,
)

stack_info = parse_stack()

# K8s stack reference + provider for the operations cluster.
cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Reference the operator stack and eagerly require its output so this stack fails
# fast if the ToolHive operator and its CRDs have not been deployed yet — the MCP
# workloads added here later cannot be reconciled without them.
operator_stack = make_stack_reference(projects.TOOLHIVE_OPERATOR, stack_info.name)
require_stack_output_value(operator_stack, "toolhive_namespace")

TOOLHIVE_NAMESPACE = "toolhive-data"

# The namespace is provisioned by the EKS operations stack; fail fast if missing.
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(TOOLHIVE_NAMESPACE, ns)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.toolhive,
    ou=BusinessUnit.operations,
    stack=stack_info,
).model_dump()

# MCPServer / VirtualMCPServer / MCPGroup resources for the data agent class are
# added in a follow-up.

export("toolhive_namespace", TOOLHIVE_NAMESPACE)
