"""Meilisearch Helm release installation and configuration for EDXApp."""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.versions import MEILISEARCH_CHART_VERSION
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_meilisearch_resources(
    stack_info: StackInfo,
    namespace: str,
) -> kubernetes.helm.v3.Release | None:
    """Create Meilisearch Helm release if enabled in configuration.

    Args:
        stack_info: Stack information with environment details
        namespace: Kubernetes namespace where Meilisearch will be deployed

    Returns:
        Meilisearch Helm release resource, or None if not enabled
    """
    meilisearch_config = Config("meilisearch")
    if not meilisearch_config.get_bool("enabled"):
        return None

    meilisearch_values: dict[str, Any] = {
        "replicaCount": meilisearch_config.get_int("replica_count") or 1,  # Default to 1 replica
        "image": {
            "pullPolicy": "IfNotPresent",
        },
        "environment": {
            "MEILI_NO_ANALYTICS": True,
            "MEILI_ENV": "production",
            "MEILI_MASTER_KEY": meilisearch_config.require_secret("master_key"),
        },
        "persistence": {
            "enabled": True,
            "size": meilisearch_config.get("pv_size") or "10Gi",
        },
        "serviceMonitor": {
            "enabled": False,
        },
        "resources": {
            "requests": {
                "cpu": meilisearch_config.get("cpu_request") or "250m",
                "memory": meilisearch_config.get("memory_request") or "512Mi",
            },
            "limits": {
                # Don't set a CPU limit per our standard practice
                "memory": meilisearch_config.get("memory_limit") or "512Mi",
            },
        },
    }

    return kubernetes.helm.v3.Release(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-helm-release-{stack_info.env_suffix}",
        kubernetes.helm.v3.ReleaseArgs(
            name="meilisearch",
            chart="meilisearch",
            version=MEILISEARCH_CHART_VERSION,
            namespace=namespace,
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://meilisearch.github.io/meilisearch-kubernetes",
            ),
            values=meilisearch_values,
            skip_await=False,
        ),
        opts=ResourceOptions(delete_before_replace=True),
    )
