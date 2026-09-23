"""Kubernetes namespace resources for the local-dev infra stack."""

from collections.abc import Callable, Iterable

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

# Namespaces created regardless of what is enabled. These predate per-app
# gating and cost almost nothing (an empty namespace plus a TLS secret), so
# they stay unconditional rather than churning every developer's stack state.
APP_NAMESPACES = (
    "mit-learn",
    "learn-ai",
    "mitxonline",
    "odl-video-service",
    "openedx",
)

# Namespaces created only when the matching app is in enabled_apps, because
# what fills them is expensive. ocw-studio brings an object store and a
# Concourse install along with it.
OPTIONAL_APP_NAMESPACES = ("ocw-studio",)


def app_namespaces_for(enabled_apps: Iterable[str]) -> tuple[str, ...]:
    """Return every app namespace to provision for this set of enabled apps.

    Shared by the modules that fan out over app namespaces (this one and
    tls.py) so they cannot drift into disagreeing about which exist.
    """
    enabled = set(enabled_apps)
    return APP_NAMESPACES + tuple(ns for ns in OPTIONAL_APP_NAMESPACES if ns in enabled)


def create_namespaces(
    _k8s: Callable[..., ResourceOptions],
    enabled_apps: Iterable[str] = (),
) -> dict[str, k8s.core.v1.Namespace]:
    """Create all namespaces required by the local-dev stack.

    Returns a dict keyed by namespace name containing every Namespace resource.
    App namespaces are pre-created here so that infra-owned Secrets (OIDC creds,
    TLS) can be placed into them before the app Tiltfile runs.
    """
    namespaces: dict[str, k8s.core.v1.Namespace] = {}

    for name in ("local-infra", "operations", *app_namespaces_for(enabled_apps)):
        namespaces[name] = k8s.core.v1.Namespace(
            f"ns-{name}",
            metadata={"name": name},
            opts=_k8s(),
        )

    return namespaces
