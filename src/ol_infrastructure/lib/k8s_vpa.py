"""Shared helper for creating VerticalPodAutoscaler CustomResource objects."""

from __future__ import annotations

from typing import Any

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
    container_name: str = "*",
    *,
    disable_other_containers: bool = False,
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

    Pass container_name to target a specific container rather than all containers
    ("*"). Targeting "*" applies the same bounds to every container in the pod,
    including sidecars such as nginx or vector, which can override their independent
    resource requests and undermine per-container rightsizing.

    Naming a single container is not by itself sufficient to leave the other
    containers alone: VPA applies its default behaviour to any container without a
    matching policy, so sidecars would still be resized. Set
    disable_other_containers=True to append a catch-all ``containerName: "*"`` policy
    with ``mode: "Off"``, which leaves every unnamed container's requests *and*
    limits exactly as declared in the pod spec -- VPA still computes
    recommendations for them, but never applies them. Only meaningful when
    container_name is not "*".
    """
    container_policies: list[dict[str, Any]] = [
        {
            "containerName": container_name,
            "controlledResources": controlled_resources,
            "controlledValues": "RequestsAndLimits",
            "minAllowed": min_allowed,
            "maxAllowed": max_allowed,
        }
    ]
    if disable_other_containers and container_name != "*":
        container_policies.append({"containerName": "*", "mode": "Off"})

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
            "resourcePolicy": {"containerPolicies": container_policies},
        },
        opts=ResourceOptions.merge(
            ResourceOptions(provider=k8s_provider)
            if k8s_provider
            else ResourceOptions(),
            opts,
        ),
    )
