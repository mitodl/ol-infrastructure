"""Install the StarRocks operator via Pulumi."""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import BusinessUnit, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
starrocks_config = Config("starrocks")

cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

namespace = "starrocks"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.starrocks,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

operator_helm_values: dict[str, Any] = {
    "global": {
        "rbac": {
            "create": True,
        }
    },
    "timeZone": "UTC",
    "starrocksOperator": {
        "enabled": True,
        "replicaCount": 1,  # the operator only needs to exist once
        "resources": {
            "limits": {"cpu": "500m", "memory": "800Mi"},
            "requests": {"cpu": "500m", "memory": "400Mi"},
        },
        "watchNamespace": namespace,  # don't waste resources monitoring all namespaces
        "log": ["--zap-time-encoding=iso8601", "--zap-encoder=console"],
    },
}

starrocks_operator_release = kubernetes.helm.v3.Release(
    "starrocks-operator",
    kubernetes.helm.v3.ReleaseArgs(
        name="starrocks-operator",
        chart="operator",
        version=STARROCKS_OPERATOR_CHART_VERSION,
        namespace=namespace,
        cleanup_on_fail=True,
        skip_await=False,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://starrocks.github.io/starrocks-kubernetes-operator",
        ),
        values=operator_helm_values,
    ),
)
