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
    # Per-component resource tuning, sized from measured usage rather than the
    # earlier guess that these are "lightweight control-plane processes".
    #
    # All three components hold cluster-wide informer caches, so memory scales
    # with POD COUNT -- including completed Job pods, which VPA never scales but
    # still caches. Measured RSS:
    #
    #   pods    recommender  updater  admission
    #   ~123     25Mi         23Mi     17Mi
    #    409     91Mi         77Mi     36Mi
    #   1250    156Mi         80Mi     64Mi
    #   8536    543Mi        192Mi    176Mi
    #
    # data-production runs ~8.5k pods because dagster Job pods are retained for
    # ttlSecondsAfterFinished=24h, and it paged on all three: the recommender
    # OOMKilled against the old 500Mi ceiling (it died ~7s in, before its Pod
    # informer finished syncing), the updater OOMKilled against 200Mi, and the
    # admission controller was sitting at 176Mi of a 200Mi limit -- the next
    # page waiting to happen. external-dns failed the same way and for the same
    # reason; see setup_external_dns.
    #
    # Requests are held near their previous values so scheduling and reserved
    # capacity on the small clusters barely move; the ceilings are what needed
    # raising. Deliberately no longer request == limit: Guaranteed QoS buys
    # eviction protection but leaves zero burst headroom, and an OOMKill is a
    # certain outage where node-pressure eviction is a rare and recoverable one.
    #
    # GOMEMLIMIT (~90% of each limit) makes the Go runtime collect harder as it
    # approaches the ceiling instead of being OOMKilled outright. The chart only
    # plumbs extraEnv into the recommender and admission controller, so the
    # updater cannot get one -- its headroom comes from the limit alone.
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
                    # Peak observed 176Mi at 8.5k pods, against a 200Mi ceiling.
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "256Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                    "extraEnv": [
                        {"name": "GOMEMLIMIT", "value": "460MiB"},
                    ],
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
                    # Heaviest of the three: it keeps usage histograms and
                    # checkpoints on top of the informer caches. Peak observed
                    # 543Mi at 8.5k pods, against a 500Mi ceiling.
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "512Mi"},
                        "limits": {"memory": "1536Mi"},
                    },
                    "extraEnv": [
                        {"name": "GOMEMLIMIT", "value": "1400MiB"},
                    ],
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
                    # Peak observed 192Mi at 8.5k pods, against a 200Mi ceiling.
                    # No GOMEMLIMIT available here -- the chart does not plumb
                    # extraEnv into this component, so the limit is the only
                    # headroom it gets.
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "256Mi"},
                        "limits": {"memory": "768Mi"},
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
