"""Install the Typesense Kubernetes Operator (TyKO) on an EKS cluster."""

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import TYPESENSE_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace

TYPESENSE_OPERATOR_NAMESPACE = "operations"


def setup_typesense_operator(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
) -> kubernetes.helm.v3.Release | None:
    """Install the Typesense Kubernetes Operator into the operations namespace.

    The operator manages TypesenseCluster CRDs and automates lifecycle
    management (config maps, secrets, statefulsets, services) for Typesense
    clusters. It is installed from the upstream Helm chart.

    Skipped when ``typesense:enable_operator`` is not set to true in config.

    Args:
        cluster_name: Name of the EKS cluster (used for Pulumi resource names).
        cluster_stack: StackReference to the EKS infrastructure stack, used to
            validate that the operations namespace exists.
        k8s_provider: Pulumi Kubernetes provider for this cluster.

    Returns:
        The Helm Release resource, or None if the operator is not enabled.
    """
    typesense_config = Config("typesense")
    if not typesense_config.get_bool("enable_operator"):
        return None

    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(TYPESENSE_OPERATOR_NAMESPACE, ns)
    )

    return kubernetes.helm.v3.Release(
        f"{cluster_name}-typesense-operator-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="typesense-operator",
            chart="typesense-operator",
            version=TYPESENSE_OPERATOR_CHART_VERSION,
            namespace=TYPESENSE_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            skip_await=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://akyriako.github.io/typesense-operator/",
            ),
            values={
                # The chart's default 256Mi limit was too tight for the
                # manager's cluster-wide informer caches: a watch
                # invalidation forcing a full relist OOMKilled it in
                # production. Give it more headroom in both directions.
                "controllerManager": {
                    "manager": {
                        "resources": {
                            "requests": {
                                "cpu": "10m",
                                "memory": "128Mi",
                            },
                            "limits": {
                                "cpu": "500m",
                                "memory": "512Mi",
                            },
                        },
                    },
                },
            },
        ),
        opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
    )
