# ruff: noqa: E501
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.versions import (
    NVIDIA_DCGM_EXPORTER_CHART_VERSION,
    NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION,
)
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    Component,
    Services,
    cluster_addon_labels,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def setup_nvidia(  # noqa: PLR0913
    cluster_name: str,
    k8s_provider: kubernetes.Provider,
    k8s_global_labels: dict[str, str],
    stack_info: StackInfo,
    nvidia_dcgm_exporter_version: str = NVIDIA_DCGM_EXPORTER_CHART_VERSION,
    nvidia_k8s_device_plugin_version: str = NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION,
):
    """
    Set up NVIDIA GPU resources including Node Feature Discovery CRDs,
    NVIDIA k8s device plugin, and DCGM exporter Helm charts.

    Args:
        cluster_name: The name of the EKS cluster.
        k8s_provider: The Pulumi Kubernetes provider instance.
        k8s_global_labels: The program's shared label dict.
        stack_info: Information about the current Pulumi stack.
        nvidia_dcgm_exporter_version: The version of the NVIDIA DCGM exporter chart.
        nvidia_k8s_device_plugin_version: The version of the NVIDIA k8s device plugin chart.
    """
    node_feature_discovery_crds = kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-nfd-crds",
        files=[
            "https://raw.githubusercontent.com/kubernetes-sigs/node-feature-discovery/master/deployment/base/nfd-crds/nfd-api-crds.yaml",
        ],
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )

    kubernetes.helm.v3.Release(
        f"{cluster_name}-nvidia-k8s-device-plugin-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="nvidia-device-plugin",
            chart="nvidia-device-plugin",
            version=nvidia_k8s_device_plugin_version,
            namespace="operations",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://nvidia.github.io/k8s-device-plugin"
            ),
            cleanup_on_fail=True,
            skip_await=True,
            values={
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "ol.mit.edu/gpu_node",
                                            "operator": "In",
                                            "values": ["true"],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "tolerations": [
                    {
                        "key": "ol.mit.edu/gpu_node",
                        "operator": "Equal",
                        "value": "true",
                        "effect": "NoSchedule",
                    }
                ],
                "gfd": {
                    "enabled": True,
                },
                "nfd": {
                    "master": {
                        "resources": {
                            "requests": {
                                "cpu": "10m",
                                "memory": "100Mi",
                            },
                            "limits": {
                                "memory": "100Mi",
                            },
                        },
                    },
                    "worker": {
                        "resources": {
                            "requests": {
                                "cpu": "5m",
                                "memory": "20Mi",
                            },
                            "limits": {
                                "memory": "20Mi",
                            },
                        },
                        "tolerations": [
                            {
                                "key": "ol.mit.edu/gpu_node",
                                "operator": "Equal",
                                "value": "true",
                                "effect": "NoSchedule",
                            },
                        ],
                    },
                },
                "config": {
                    "map": {
                        "default": "version: v1\nsharing:\n  mps:\n    resources:\n    - name: nvidia.com/gpu\n      replicas: 10\n    failRequestsGreaterThanOne: false\n",
                    }
                },
                "resources": {
                    "requests": {
                        "cpu": "10m",
                        "memory": "100Mi",
                    },
                    "limits": {
                        "memory": "100Mi",
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=k8s_provider,
            delete_before_replace=True,
        ),
    )

    kubernetes.helm.v3.Release(
        f"{cluster_name}-nvidia-dcgm-exporter-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="nvidia-dcgm-exporter",
            chart="dcgm-exporter",
            version=nvidia_dcgm_exporter_version,
            namespace="operations",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://nvidia.github.io/dcgm-exporter/helm-charts"
            ),
            cleanup_on_fail=True,
            skip_await=True,
            values={
                "tolerations": [
                    {
                        "key": "ol.mit.edu/gpu_node",
                        "operator": "Equal",
                        "value": "true",
                        "effect": "NoSchedule",
                    }
                ],
                "resources": {
                    "requests": {
                        "cpu": "10m",
                        "memory": "512Mi",
                    },
                    "limits": {
                        "memory": "512Mi",
                    },
                },
                "nodeSelector": {
                    "ol.mit.edu/gpu_node": "true",
                },
                "podLabels": cluster_addon_labels(
                    base_labels=k8s_global_labels,
                    stack_info=stack_info,
                    service=Services.dcgm_exporter,
                    component=Component.exporter,
                    # GPU telemetry. Nothing user-facing depends on it.
                    alert_tier=AlertTier.ticket,
                ),
                "serviceMonitor": {
                    "enabled": False,
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=k8s_provider,
            depends_on=[node_feature_discovery_crds],
            delete_before_replace=True,
        ),
    )
