from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import QDRANT_CHART_VERSION
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

qdrant_config = Config("qdrant")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

qdrant_namespace = qdrant_config.get("namespace") or "qdrant"
cluster_stack.require_output("namespaces").apply(
    lambda namespaces: check_cluster_namespace(qdrant_namespace, namespaces)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.qdrant,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

release_name = "qdrant"
domain = qdrant_config.require("domain")
cert_secret_name = f"{release_name}-tls"

replica_count = qdrant_config.get_int("replica_count") or 3
persistence_size = qdrant_config.get("persistence_size") or "200Gi"
snapshot_size = qdrant_config.get("snapshot_size") or persistence_size
snapshot_enabled = qdrant_config.get_bool("snapshot_persistence_enabled")
if snapshot_enabled is None:
    snapshot_enabled = True

cluster_enabled = qdrant_config.get_bool("cluster_enabled")
if cluster_enabled is None:
    cluster_enabled = True

service_type = qdrant_config.get("service_type") or "NodePort"
service_annotations = qdrant_config.get_object("service_annotations") or {}

enable_topology_spread = qdrant_config.get_bool("enable_topology_spread")  # Important
if enable_topology_spread is None:
    enable_topology_spread = True

topology_spread_constraints: list[dict[str, Any]] = []
if enable_topology_spread:
    topology_spread_constraints = [
        {
            "maxSkew": 1,
            "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "qdrant",
                    "app.kubernetes.io/instance": release_name,
                },
            },
        }
    ]

pod_disruption_budget_enabled = qdrant_config.get_bool("enable_pod_disruption_budget")
if pod_disruption_budget_enabled is None:
    pod_disruption_budget_enabled = True

pod_disruption_budget = (
    {
        "enabled": True,
        "maxUnavailable": qdrant_config.get_int("pod_disruption_budget_max_unavailable")
        or 1,
    }
    if pod_disruption_budget_enabled
    else {"enabled": False}
)

resources = {
    "requests": {
        "cpu": qdrant_config.get("cpu_request") or "2000m",
        "memory": qdrant_config.get("memory_request") or "8Gi",
    },
    "limits": {
        "cpu": qdrant_config.get("cpu_limit") or "4000m",
        "memory": qdrant_config.get("memory_limit") or "15Gi",
    },
}

persistence_values: dict[str, Any] = {
    "size": persistence_size,
    "accessModes": ["ReadWriteOnce"],
}
storage_class = qdrant_config.get("storage_class_name")
if storage_class:
    persistence_values["storageClassName"] = storage_class

snapshot_persistence_values: dict[str, Any] = {
    "enabled": snapshot_enabled,
    "size": snapshot_size,
    "accessModes": ["ReadWriteOnce"],
}
snapshot_storage_class = qdrant_config.get("snapshot_storage_class_name")
if snapshot_storage_class:
    snapshot_persistence_values["storageClassName"] = snapshot_storage_class

service_monitor_enabled = qdrant_config.get_bool("enable_service_monitor") or False
service_monitor_additional_labels = (
    qdrant_config.get_object("service_monitor_additional_labels") or {}
)

qdrant_values: dict[str, Any] = {
    "commonLabels": k8s_global_labels,
    "podLabels": k8s_global_labels,
    "additionalLabels": k8s_global_labels,
    "replicaCount": replica_count,
    "service": {
        "type": service_type,
        "annotations": service_annotations,
        "additionalLabels": k8s_global_labels,
    },
    "persistence": persistence_values,
    "snapshotPersistence": snapshot_persistence_values,
    "resources": resources,
    "topologySpreadConstraints": topology_spread_constraints,
    "podDisruptionBudget": pod_disruption_budget,
    "config": {
        "cluster": {
            "enabled": cluster_enabled,
        },
    },
    "apiKey": qdrant_config.require_secret("api_key"),
    "metrics": {
        "serviceMonitor": {
            "enabled": service_monitor_enabled,
            "additionalLabels": service_monitor_additional_labels,
        },
    },
    "ingress": {"enabled": False},
}

qdrant_application = kubernetes.helm.v3.Release(
    f"{release_name}-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name=release_name,
        chart="qdrant",
        version=QDRANT_CHART_VERSION,
        namespace=qdrant_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://qdrant.github.io/qdrant-helm",
        ),
        values=qdrant_values,
        skip_await=False,
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

should_create_gateway = qdrant_config.get_bool("create_gateway")
if should_create_gateway is None:
    should_create_gateway = True

if should_create_gateway:
    gateway_config = OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name=release_name,
        labels=k8s_global_labels,
        namespace=qdrant_namespace,
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https",
                hostname=domain,
                port=8443,
                tls_mode="Terminate",
                certificate_secret_name=cert_secret_name,
                certificate_secret_namespace=qdrant_namespace,
            )
        ],
        routes=[
            OLEKSGatewayRouteConfig(
                backend_service_name=release_name,
                backend_service_namespace=qdrant_namespace,
                backend_service_port=6333,
                hostnames=[domain],
                name=f"{release_name}-https",
                listener_name="https",
                port=8443,
            )
        ],
    )

    OLEKSGateway(
        f"{release_name}-{stack_info.name}-gateway",
        gateway_config=gateway_config,
        opts=ResourceOptions(
            delete_before_replace=True,
        ),
    )
