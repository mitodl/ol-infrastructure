# ruff: noqa: PLR0913
"""Install the Vertical Pod Autoscaler (VPA) as a core cluster capability.

Uses the official Helm chart published by the Kubernetes autoscaler project at
https://kubernetes.github.io/autoscaler.

VPA is installed with all three components enabled:
- recommender: analyses historical resource usage and generates recommendations
- updater: evicts pods whose resources differ significantly from recommendations
- admissionController: mutates new pod resource requests to match recommendations

The chart is installed in the kube-system namespace, consistent with other
cluster-level infrastructure components (metrics-server, kube-state-metrics, etc.).
"""

import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions


def setup_vpa(
    cluster_name: str,
    cluster: eks.Cluster,
    k8s_provider: kubernetes.Provider,
    node_groups: list[eks.NodeGroupV2],
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
) -> kubernetes.helm.v3.Release:
    """
    Install the Vertical Pod Autoscaler as a core cluster capability.

    :param cluster_name: The name of the EKS cluster.
    :param cluster: The EKS cluster object.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param node_groups: A list of EKS node groups.
    :param k8s_global_labels: A dictionary of global labels to apply to resources.
    :param operations_tolerations: Tolerations for scheduling on
        operations-tainted nodes.
    :param versions: A dictionary of component versions keyed by component name.
    :returns: The Helm Release resource, for use as a dependency by VPA objects.
    """
    # Per-component resource tuning. VPA components are lightweight control-plane
    # processes; these limits are conservative starting points and can be adjusted
    # once real-world usage is observed via VPA recommendations on VPA itself.
    return kubernetes.helm.v3.Release(
        f"{cluster_name}-vpa-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="vertical-pod-autoscaler",
            chart="vertical-pod-autoscaler",
            version=versions["VPA_CHART"],
            namespace="kube-system",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://kubernetes.github.io/autoscaler",
            ),
            values={
                "commonLabels": k8s_global_labels,
                "admissionController": {
                    "enabled": True,
                    "replicas": 2,
                    "tolerations": operations_tolerations,
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "200Mi"},
                        "limits": {"memory": "200Mi"},
                    },
                    # certGen (the chart default) is the preferred TLS strategy.
                    # A pre-install hook job creates the vpa-tls-certs Secret
                    # (self-signed CA + cert/key), Helm creates the
                    # MutatingWebhookConfiguration, and a post-install hook patches
                    # the caBundle into it.  registerWebhook is left at its default
                    # (false) so the admission controller does not attempt to manage
                    # the webhook itself — that path would require granting it
                    # cluster-wide delete on mutatingwebhookconfigurations, which is
                    # a significant privilege escalation risk.
                    # Ignore failures so a VPA webhook outage does not block pod
                    # creation cluster-wide. Pods will simply start without VPA
                    # mutation applied and the next eviction cycle will correct them.
                    "mutatingWebhookConfiguration": {
                        "failurePolicy": "Ignore",
                    },
                },
                "recommender": {
                    "enabled": True,
                    "replicas": 1,
                    "tolerations": operations_tolerations,
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "500Mi"},
                        "limits": {"memory": "500Mi"},
                    },
                },
                "updater": {
                    "enabled": True,
                    "replicas": 1,
                    "tolerations": operations_tolerations,
                    # InPlaceOrRecreate (in-place resize, falls back to eviction)
                    # was promoted to GA and enabled by default in VPA 1.6, so no
                    # feature gate is required. "InPlace" is a separate, alpha-only
                    # mode added in VPA 1.7 that never evicts and requires K8s 1.33+
                    # with the cluster-level InPlacePodVerticalScaling gate - do not
                    # set --feature-gates=InPlace=true here, it would silently stop
                    # falling back to eviction when a resize isn't feasible.
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "200Mi"},
                        "limits": {"memory": "200Mi"},
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=cluster,
            depends_on=[cluster, *node_groups],
            delete_before_replace=True,
        ),
    )
