"""Install the StarRocks operator via Pulumi."""

from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    ecr_image_uri,
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
        "image": {
            "repository": ecr_image_uri("starrocks/operator"),
            "pullPolicy": "IfNotPresent",
        },
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

# Create secret for StarRocks password
starrocks_password_secret = kubernetes.core.v1.Secret(
    "starrocks-root-password",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="starrocks-root-password",
        namespace=namespace,
        labels=k8s_global_labels,
    ),
    string_data={
        "password": starrocks_config.require_secret("root_password"),
    },
)

kube_starrocks_helm_values: dict[str, Any] = {
    "timeZone": "UTC",
    "datadog": {
        "enabled": False,
    },
    "serviceMonitor": {
        "enabled": False,
    },
    "starrocksCluster": {
        "name": "ol-starrocks",
        "namespace": namespace,
        "enabledCn": False,
        "annotations": k8s_global_labels,
        "starrocksFESpec": {
            "image": "starrocks/fe-ubuntu:latest",
            "replicas": 3,
            "runAsNonRoot": True,
            "service": {
                "type": "ClusterIP",
            },
        },
        "starrocksBeSpec": {
            "image": "starrocks/be-ubuntu:latest",
            "replicas": 3,
            "runAsNonRoot": True,
            "service": {
                "type": "ClusterIP",
            },
        },
    },
    "initPassword": {
        "enabled": True,
        "passwordSecret": "starrocks-root-password",  # pragma: allowlist secret
    },
}

kube_starrocks_release = kubernetes.helm.v3.Release(
    "kube-starrocks",
    kubernetes.helm.v3.ReleaseArgs(
        name="kube-starrocks",
        chart="kube-starrocks",
        version=STARROCKS_OPERATOR_CHART_VERSION,
        namespace=namespace,
        cleanup_on_fail=True,
        skip_await=False,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://starrocks.github.io/starrocks-kubernetes-operator",
        ),
        values=kube_starrocks_helm_values,
    ),
    opts=pulumi.ResourceOptions(
        depends_on=[starrocks_operator_release, starrocks_password_secret],
    ),
)
