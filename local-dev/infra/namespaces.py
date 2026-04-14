"""Kubernetes namespace resources for the local-dev infra stack."""

from collections.abc import Callable

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

APP_NAMESPACES = ("mit-learn", "learn-ai", "mitxonline", "odl-video-service")


def create_namespaces(
    _k8s: Callable[..., ResourceOptions],
) -> dict[str, k8s.core.v1.Namespace]:
    """Create all namespaces required by the local-dev stack.

    Returns a dict keyed by namespace name containing every Namespace resource.
    App namespaces are pre-created here so that infra-owned Secrets (OIDC creds,
    TLS) can be placed into them before the app Tiltfile runs.
    """
    namespaces: dict[str, k8s.core.v1.Namespace] = {}

    for name in ("local-infra", "operations", *APP_NAMESPACES):
        namespaces[name] = k8s.core.v1.Namespace(
            f"ns-{name}",
            metadata={"name": name},
            opts=_k8s(),
        )

    return namespaces
