import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace


def setup_starrocks(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
):
    """
    Set up StarRocks operator resources including Helm chart installation.

    Only installs if starrocks.enable_operator is set to true in configuration.

    Args:
        cluster_name: The name of the EKS cluster.
        cluster_stack: A StackReference to the EKS cluster stack.
        k8s_provider: The Pulumi Kubernetes provider instance.
    """
    starrocks_config = Config("starrocks")
    if not starrocks_config.get_bool("enable_operator"):
        return

    starrocks_namespace = "starrocks"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(starrocks_namespace, ns)
    )

    kubernetes.helm.v3.Release(
        f"{cluster_name}-starrocks-operator-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="starrocks-operator",
            chart="operator",
            version=STARROCKS_OPERATOR_CHART_VERSION,
            namespace=starrocks_namespace,
            cleanup_on_fail=True,
            skip_await=False,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://starrocks.github.io/starrocks-kubernetes-operator",
            ),
            values={
                "global": {
                    "rbac": {
                        "create": True,
                    },
                },
                "timeZone": "UTC",
                "nameOverride": "starrocks-operator",
                "starrocksOperator": {
                    "enabled": True,
                    "imagePullPolicy": "IfNotPresent",
                    "replicaCount": 1,
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "128Mi",
                        },
                        "limits": {
                            "memory": "128Mi",
                        },
                    },
                },
            },
        ),
        opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
    )
