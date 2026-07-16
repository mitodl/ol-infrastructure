"""Meilisearch Helm release installation and configuration for EDXApp."""

from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.versions import MEILISEARCH_CHART_VERSION, MEILISEARCH_VERSION
from bridge.secrets.sops import read_yaml_secrets
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
        "custom_labels": k8s_global_labels,
        "environment": {
            "MEILI_NO_ANALYTICS": True,
            "MEILI_ENV": "production",
            "MEILI_MASTER_KEY": secrets["meilisearch_master_key"],
        },
        # Meilisearch is a single-replica StatefulSet backed by a ReadWriteOnce
        # EBS volume. Every time its node is rotated the volume must fully detach
        # from the old node before it can attach to the new one, producing a
        # multi-minute window with zero ready endpoints (AWS "Multi-Attach error"
        # / FailedAttachVolume). The only durable fix is to make the pod's node
        # stop rotating so often, so we harden against both drivers of churn:
        #   1. karpenter.sh/do-not-disrupt blocks Karpenter's *voluntary*
        #      disruption (consolidation, drift, emptiness) — the default
        #      NodePool consolidates aggressively (consolidateAfter: 10m), which
        #      would otherwise drain this pod's node repeatedly.
        #   2. Pinning to on-demand capacity removes *spot-interruption*
        #      evictions, the other involuntary churn source.
        "podAnnotations": {
            "karpenter.sh/do-not-disrupt": "true",
        },
        "nodeSelector": {
            "karpenter.sh/capacity-type": meilisearch_config.get("node_capacity_type")
            or "on-demand",
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
