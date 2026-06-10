"""Shared helper for creating VerticalPodAutoscaler CustomResource objects."""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions


def make_vpa(  # noqa: PLR0913
    name: str,
    namespace: str,
    target_kind: str,
    target_name: pulumi.Input[str],
    controlled_resources: list[str],
    min_allowed: dict[str, str],
    max_allowed: dict[str, str],
    k8s_provider: kubernetes.Provider | None = None,
    opts: ResourceOptions | None = None,
) -> kubernetes.apiextensions.CustomResource:
    """Create a VerticalPodAutoscaler CustomResource.

    Uses InPlaceOrRecreate mode unconditionally: attempts live resize (K8s 1.33+,
    VPA 1.6+) and falls back to pod eviction only when an in-place update cannot
    be applied.

    When a CPU-based HPA is present on the same target, pass
    controlled_resources=["memory"] to avoid the known HPA/VPA conflict where
    VPA-adjusted CPU requests distort the utilization percentage the HPA observes.
    """
    return kubernetes.apiextensions.CustomResource(
        name,
        api_version="autoscaling.k8s.io/v1",
        kind="VerticalPodAutoscaler",
        metadata={
            "name": name,
            "namespace": namespace,
        },
        spec={
            "targetRef": {
                "apiVersion": "apps/v1",
                "kind": target_kind,
                "name": target_name,
            },
            "updatePolicy": {
                "updateMode": "InPlaceOrRecreate",
            },
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": "*",
                        "controlledResources": controlled_resources,
                        "controlledValues": "RequestsAndLimits",
                        "minAllowed": min_allowed,
                        "maxAllowed": max_allowed,
                    }
                ]
            },
        },
        opts=ResourceOptions.merge(
            ResourceOptions(provider=k8s_provider)
            if k8s_provider
            else ResourceOptions(),
            opts,
        ),
    )
