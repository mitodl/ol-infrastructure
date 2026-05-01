"""Valkey (Redis-compatible) cache for the local-dev infra stack."""

from collections.abc import Callable

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


def create_cache(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
) -> k8s.apps.v1.StatefulSet:
    """Deploy a single-node Valkey StatefulSet.

    Returns the StatefulSet resource; callers that need the cache as a
    dependency should depend on this resource.
    """
    valkey_sts = k8s.apps.v1.StatefulSet(
        "valkey",
        metadata={"name": "valkey", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "valkey"}},
            "serviceName": "valkey",
            "template": {
                "metadata": {"labels": {"app": "valkey"}},
                "spec": {
                    "containers": [
                        {
                            "name": "valkey",
                            "image": "valkey/valkey:8-alpine",
                            "ports": [{"containerPort": 6379}],
                            "resources": {
                                "limits": {"memory": "128Mi"},
                            },
                        }
                    ]
                },
            },
        },
        opts=_k8s(parent=local_infra_ns),
    )

    k8s.core.v1.Service(
        "valkey-svc",
        metadata={"name": "valkey", "namespace": "local-infra"},
        spec={
            "selector": {"app": "valkey"},
            "ports": [{"port": 6379, "targetPort": 6379}],
            "clusterIP": "None",  # headless for StatefulSet
        },
        opts=_k8s(parent=valkey_sts),
    )

    return valkey_sts
