"""Meilisearch Helm release installation and configuration for EDXApp."""

from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.versions import MEILISEARCH_CHART_VERSION, MEILISEARCH_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.apisix import (
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_meilisearch_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
) -> kubernetes.helm.v3.Release | None:
    """Create Meilisearch Helm release if enabled in configuration.

    Args:
        stack_info: Stack information with environment details
        namespace: Kubernetes namespace where Meilisearch will be deployed

    Returns:
        Meilisearch Helm release resource, or None if not enabled
    """
    meilisearch_config = Config("meilisearch")
    if not meilisearch_config.get_bool("deploy"):
        return None

    tls_secret_name = "meilisearch-tls-pair"  # pragma: allowlist secret  # noqa: S105
    OLCertManagerCert(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-cert-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="meilisearch",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[meilisearch_config.require("domain")],
        ),
    )

    meilisearch_shared_plugins = OLApisixSharedPlugins(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-shared-plugins-{stack_info.env_suffix}",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="meilisearch",
            resource_suffix="ol-shared-plugins",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            # OLApisixHTTPRoute attaches only the shared PluginConfig via
            # ExtensionRef when shared_plugin_config_name is set, ignoring the
            # route's own `plugins` list entirely -- including the request-id
            # plugin OLApisixHTTPRouteConfig's validator would otherwise add.
            # Include it here explicitly so tracing/correlation still works.
            plugins=[
                {
                    "name": "request-id",
                    "enable": True,
                    "config": {"include_in_response": True},
                },
            ],
        ),
    )

    OLApisixHTTPRoute(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-httproute-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="meilisearch",
                hosts=[meilisearch_config.require("domain")],
                paths=["/*"],
                backend_service_name="meilisearch",
                backend_service_port=7700,
                shared_plugin_config_name=meilisearch_shared_plugins.resource_name,
                plugins=[],
            ),
        ],
    )

    secrets = read_yaml_secrets(
        Path(f"edxapp/{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
    )

    meilisearch_values: dict[str, Any] = {
        "replicaCount": meilisearch_config.get_int("replica_count")
        or 1,  # Default to 1 replica
        "image": {
            "tag": MEILISEARCH_VERSION,
            "pullPolicy": "IfNotPresent",
        },
        "customLabels": k8s_global_labels,
        "environment": {
            "MEILI_NO_ANALYTICS": True,
            "MEILI_ENV": "production",
            "MEILI_MASTER_KEY": secrets["meilisearch_master_key"],
            # Without this, Meilisearch refuses to start whenever the on-disk
            # database was written by a different engine version, which is what
            # forced the v1.33.0 image pin in March 2026. It migrates the
            # database in place on startup and is a no-op once the versions
            # match, so it is safe to leave enabled permanently. Note that this
            # is one-way: Meilisearch has no downgrade path, so rolling the
            # image tag back requires restoring the volume from a snapshot.
            "MEILI_UPGRADE_DB": True,
        },
        # The chart's default startup budget is 60s (60 x 1s). A version
        # upgrade migrates the task queue synchronously before the HTTP server
        # binds, so that default can kill the pod mid-migration and leave the
        # database partially converted. Give it 15 minutes instead.
        "startupProbe": {
            "periodSeconds": 5,
            "failureThreshold": 180,
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
        # Pin to the static on-demand core nodegroup instead of the
        # Karpenter spot fleet: its PVC is zone-locked once bound, and spot
        # rebalance-recommendation waves were cordoning nodes faster than
        # the pod could reach Ready (2026-07-27 TMM).
        "nodeSelector": {
            "ol.mit.edu/core_node": "true",
        },
    }

    # Meilisearch sizes its indexing buffer at two thirds of the machine's total
    # memory, measured with the sysinfo crate, which reads the host's
    # /proc/meminfo and not the cgroup limit. On the m8i-flex.4xlarge core nodes
    # that means it budgets ~40Gi inside whatever memory_limit we set, so it
    # never flushes early and leaves the cgroup to reclaim instead. Pin it so
    # the budget and the limit agree. Only emitted when configured, so the
    # stacks that have not been sized for it render unchanged.
    if max_indexing_memory := meilisearch_config.get("max_indexing_memory"):
        meilisearch_values["environment"]["MEILI_MAX_INDEXING_MEMORY"] = (
            max_indexing_memory
        )

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
