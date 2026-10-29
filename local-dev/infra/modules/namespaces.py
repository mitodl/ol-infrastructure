"""Kubernetes namespace resources for the local-dev infra stack."""

from collections.abc import Callable, Iterable

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

# Namespaces created regardless of what is enabled. Most predate per-app
# gating and cost almost nothing (an empty namespace plus a TLS secret), so
# they stay unconditional rather than churning every developer's stack state.
# gitlab is here rather than in OPTIONAL_APP_NAMESPACES despite being an
# opt-in app. Gating the namespace would delete it when the app is switched
# off, and deleting a namespace takes its PersistentVolumeClaims with it --
# StatefulSet volume retention cannot survive that.
#
# What that would cost is GitLab's own git history, plus a first boot (~2
# minutes) on every re-enable. It is not the only copy of the content:
# Studio's is in the shared Postgres, and it can rebuild a repo from there with
# reset_sync_states --create_backends and sync_website_to_backend
# --force-create. Avoiding that rebuild, and the boot, is the reason this
# namespace is unconditional; the volumes are reclaimed explicitly, as
# documented in the GitLab section of local-dev/README.md.
APP_NAMESPACES = (
    "mit-learn",
    "learn-ai",
    "mitxonline",
    "odl-video-service",
    "openedx",
    "gitlab",
)

# Namespaces created only when the matching app is in enabled_apps, because
# what fills them is expensive. ocw-studio brings an object store and a
# Concourse install along with it. micromasters and ol-analytics-api are here
# because they were added after per-app gating existed, so an unconditional
# namespace would churn every stack for an app most developers do not run.
# Deployed, ol-analytics-api runs in the ol-analytics namespace; here the
# namespace is named after the app, which is what this gating keys on.
OPTIONAL_APP_NAMESPACES = ("ocw-studio", "micromasters", "ol-analytics-api")


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
