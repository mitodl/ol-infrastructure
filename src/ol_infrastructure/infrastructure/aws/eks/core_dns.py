"""Pulumi resources for CoreDNS in EKS."""

from __future__ import annotations

import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions


def create_core_dns_resources(
    cluster_name: str,
    k8s_global_labels: dict[str, str],
    k8s_provider: kubernetes.Provider,
    cluster: eks.Cluster,
    node_groups: list[eks.NodeGroupV2],
):
    """
    Create resources for ensuring CoreDNS resiliency.

    Includes a PodDisruptionBudget and a DeploymentPatch to increase replicas and
    set an update strategy.
    """

    # PodDisruptionBudget to ensure at least one CoreDNS pod is always available
    # Generally more than one. This is crucial for cluster DNS reliability.
    kubernetes.policy.v1.PodDisruptionBudget(
        f"{cluster_name}-coredns-pdb",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="coredns",
            namespace="kube-system",
            labels=k8s_global_labels,
        ),
        spec=kubernetes.policy.v1.PodDisruptionBudgetSpecArgs(
            max_unavailable=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={
                    "eks.amazonaws.com/component": "coredns",
                    "k8s-app": "kube-dns",
                }
            ),
        ),
        opts=ResourceOptions(provider=k8s_provider, parent=cluster),
    )

    # Modifying the coredns plugin is a huge pain, so we just patch the existing
    # deployment object to increase replicas and set a rolling update strategy.
    kubernetes.apps.v1.DeploymentPatch(
        f"{cluster_name}-coredns-resiliency-patch",
        api_version="apps/v1",
        kind="Deployment",
        metadata={
            "name": "coredns",
            "namespace": "kube-system",
            "annotations": {"pulumi.com/patchForce": "true"},
        },
        spec={
            "replicas": 3,  # Ensure at least 3 replicas for resiliency
            "strategy": {
                "rollingUpdate": {
                    "maxUnavailable": 1,
                    "maxSurge": "100%",  # Allow up to 6 instances during update
                },
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider, parent=cluster, depends_on=[*node_groups]
        ),
    )
