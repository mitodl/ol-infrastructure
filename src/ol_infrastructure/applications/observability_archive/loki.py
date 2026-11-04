"""The Loki release: the archive's log backend (plan D5).

Official `loki` chart in distributed mode at replication factor 1 on the
single-zone archive node group, TSDB v13 on the archive bucket through IRSA,
compactor-enforced retention with the delete API on for erasure requests, and
structured metadata on so OTLP logs land intact. QA runs the same code as a
30-day canary with the smallest counts that still exercise every component
(plan D2).
"""

from dataclasses import dataclass

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from bridge.lib.versions import LOKI_CHART_VERSION
from ol_infrastructure.lib.aws.eks_helper import ecr_dockerhub_registry
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    AWSBase,
    Services,
    cluster_addon_labels,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

RELEASE_NAME = "loki"
HTTP_PORT = 3100
# Pinned explicitly rather than inherited from the chart's appVersion so a
# chart bump and a Loki bump are separate, reviewable diffs.
LOKI_IMAGE_TAG = "3.6.11"
# The day the schema took effect. Never move it: Loki reads the index for a
# period with the schema that was in force when it was written.
SCHEMA_FROM = "2026-11-04"
STORAGE_CLASS = "ebs-gp3-sc"


@dataclass(frozen=True)
class LokiSizing:
    """Replica counts, resources and limits for one environment."""

    ingesters: int
    distributors: int
    queriers: int
    index_gateways: int
    ingester_memory_request: str
    ingester_memory_limit: str
    ingester_wal_size: str
    querier_memory_request: str
    querier_memory_limit: str
    chunks_cache_mb: int
    results_cache_mb: int
    ingestion_rate_mb: int
    ingestion_burst_size_mb: int
    max_global_streams_per_user: int


# Production carries ~119 GB/day (ADR-0014). Each ingester holds its open
# chunks in memory for up to max_chunk_age (2h), so memory scales with volume
# per ingester, and the WAL claim holds the same window on disk. QA is sized to
# prove the pipeline, not to hold volume.
SIZING: dict[str, LokiSizing] = {
    "qa": LokiSizing(
        ingesters=2,
        distributors=2,
        queriers=2,
        index_gateways=1,
        ingester_memory_request="1Gi",
        ingester_memory_limit="3Gi",
        ingester_wal_size="20Gi",
        querier_memory_request="512Mi",
        querier_memory_limit="2Gi",
        chunks_cache_mb=1024,
        results_cache_mb=512,
        ingestion_rate_mb=16,
        ingestion_burst_size_mb=32,
        max_global_streams_per_user=50000,
    ),
    "production": LokiSizing(
        ingesters=3,
        distributors=2,
        queriers=2,
        index_gateways=1,
        ingester_memory_request="3Gi",
        ingester_memory_limit="8Gi",
        ingester_wal_size="50Gi",
        querier_memory_request="1Gi",
        querier_memory_limit="4Gi",
        chunks_cache_mb=2048,
        results_cache_mb=1024,
        ingestion_rate_mb=32,
        ingestion_burst_size_mb=64,
        max_global_streams_per_user=100000,
    ),
}


@dataclass
class LokiRelease:
    """What the ingress routes and the Cloud datasource point at."""

    release: kubernetes.helm.v3.Release
    distributor_service: str
    query_frontend_service: str
    http_port: int = HTTP_PORT


def build(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    aws_config: AWSBase,
    k8s_global_labels: dict[str, str],
    namespace: str,
    bucket_name: str,
    irsa_role_arn: Output[str],
    service_account: str,
    retention_days: int,
    node_selector: dict[str, str],
    tolerations: list[dict[str, str]],
    depends_on: list[Resource],
) -> LokiRelease:
    """Install Loki in distributed mode on the archive node group."""
    env = stack_info.env_suffix
    sizing = SIZING[env]
    registry = ecr_dockerhub_registry()

    # One label set for every pod the chart renders. The archive's alerts are
    # tickets (ADR-0014 step 6); the chart has no per-component label value
    # that stays out of a selector, so the role is left off.
    pod_labels = cluster_addon_labels(
        base_labels=k8s_global_labels,
        stack_info=stack_info,
        service=Services.telemetry_archive,
        alert_tier=AlertTier.ticket,
    )

    def placement(**extra: object) -> dict[str, object]:
        return {
            "nodeSelector": node_selector,
            "tolerations": tolerations,
            "podLabels": pod_labels,
            **extra,
        }

    def persistence(size: str) -> dict[str, object]:
        return {
            "enabled": True,
            "claims": [
                {
                    "name": "data",
                    "accessModes": ["ReadWriteOnce"],
                    "size": size,
                    "storageClass": STORAGE_CLASS,
                }
            ],
        }

    values: dict[str, object] = {
        "deploymentMode": "Distributed",
        "global": {"imageRegistry": registry},
        "loki": {
            # Writers and the PDC read path bypass any gateway, so nothing
            # would set X-Scope-OrgID; one tenant it is.
            "auth_enabled": False,
            "image": {"repository": "grafana/loki", "tag": LOKI_IMAGE_TAG},
            "podLabels": pod_labels,
            "commonConfig": {
                "replication_factor": 1,
                "path_prefix": "/var/loki",
            },
            "storage": {
                "type": "s3",
                "bucketNames": {
                    "chunks": bucket_name,
                    "ruler": bucket_name,
                    "admin": bucket_name,
                },
                "s3": {"region": aws_config.region},
            },
            "schemaConfig": {
                "configs": [
                    {
                        "from": SCHEMA_FROM,
                        "store": "tsdb",
                        "object_store": "s3",
                        "schema": "v13",
                        "index": {"prefix": "loki_index_", "period": "24h"},
                    }
                ]
            },
            "limits_config": {
                "retention_period": f"{retention_days}d",
                # OTLP resource attributes outside Loki's default index set
                # become structured metadata instead of being dropped.
                "allow_structured_metadata": True,
                "volume_enabled": True,
                "reject_old_samples": True,
                "reject_old_samples_max_age": "168h",
                "ingestion_rate_mb": sizing.ingestion_rate_mb,
                "ingestion_burst_size_mb": sizing.ingestion_burst_size_mb,
                "per_stream_rate_limit": "8MB",
                "per_stream_rate_limit_burst": "32MB",
                "max_global_streams_per_user": sizing.max_global_streams_per_user,
                "max_query_series": 5000,
                # Loki's default is 721h; anything longer is a 400 before the
                # query is split, which would make the retention unreachable.
                "max_query_length": f"{retention_days + 1}d",
                "query_timeout": "300s",
                "split_queries_by_interval": "15m",
            },
            "compactor": {
                "working_directory": "/var/loki/compactor",
                "compaction_interval": "10m",
                "retention_enabled": True,
                # Chunks referenced by a delete request or past retention are
                # removed this long after the index is rewritten, so an
                # in-flight query never loses a chunk under it.
                "retention_delete_delay": "2h",
                "retention_delete_worker_count": 150,
                "delete_request_store": "s3",
            },
            "ingester": {"chunk_encoding": "snappy"},
            "querier": {"max_concurrent": 4},
            "tracing": {"enabled": False},
            "analytics": {"reporting_enabled": False},
        },
        "serviceAccount": {
            "create": True,
            "name": service_account,
            "annotations": {"eks.amazonaws.com/role-arn": irsa_role_arn},
        },
        # Replication factor 1 on one zone (plan D3): the chart's default of
        # three zone-aware ingester StatefulSets would schedule two of them
        # nowhere.
        "ingester": placement(
            replicas=sizing.ingesters,
            zoneAwareReplication={"enabled": False},
            persistence=persistence(sizing.ingester_wal_size),
            resources={
                "requests": {"cpu": "500m", "memory": sizing.ingester_memory_request},
                "limits": {"memory": sizing.ingester_memory_limit},
            },
        ),
        "distributor": placement(
            replicas=sizing.distributors,
            maxUnavailable=1,
            resources={
                "requests": {"cpu": "250m", "memory": "256Mi"},
                "limits": {"memory": "1Gi"},
            },
        ),
        "querier": placement(
            replicas=sizing.queriers,
            maxUnavailable=1,
            resources={
                "requests": {"cpu": "500m", "memory": sizing.querier_memory_request},
                "limits": {"memory": sizing.querier_memory_limit},
            },
        ),
        "queryFrontend": placement(
            replicas=1,
            resources={
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"memory": "1Gi"},
            },
        ),
        "queryScheduler": placement(
            replicas=1,
            resources={
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"memory": "512Mi"},
            },
        ),
        "indexGateway": placement(
            replicas=sizing.index_gateways,
            # The one StatefulSet in the chart with the flat persistence shape.
            persistence={
                "enabled": True,
                "size": "20Gi",
                "storageClass": STORAGE_CLASS,
            },
            resources={
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"memory": "2Gi"},
            },
        ),
        "compactor": placement(
            replicas=1,
            persistence=persistence("20Gi"),
            resources={
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"memory": "2Gi"},
            },
        ),
        # Memcached at the smallest useful size; the chart defaults are sized
        # for a cluster an order of magnitude busier.
        "chunksCache": placement(allocatedMemory=sizing.chunks_cache_mb),
        "resultsCache": placement(allocatedMemory=sizing.results_cache_mb),
        "memcached": {"image": {"repository": "library/memcached"}},
        # Nothing from the other deployment modes, and none of the optional
        # components: no ruler (alerts never read the archive, plan D13), no
        # nginx gateway (writers come through the archive's Traefik and the
        # PDC agent reads the query-frontend Service directly), no canary or
        # chart test, no bloom or pattern ingesters.
        "ruler": {"enabled": False},
        "gateway": {"enabled": False},
        "test": {"enabled": False},
        "lokiCanary": {"enabled": False},
        "backend": {"replicas": 0},
        "read": {"replicas": 0},
        "write": {"replicas": 0},
        "singleBinary": {"replicas": 0},
        "bloomPlanner": {"replicas": 0},
        "bloomBuilder": {"replicas": 0},
        "bloomGateway": {"replicas": 0},
        "patternIngester": {"replicas": 0},
        "minio": {"enabled": False},
        # The operations cluster's prometheusOperatorObjects feature picks the
        # ServiceMonitor up and ships Loki's own metrics to Grafana Cloud. The
        # chart's MetricsInstance and self-monitoring need the Grafana Agent
        # operator, which this cluster does not run.
        "monitoring": {
            "dashboards": {"enabled": False},
            "rules": {"enabled": False},
            "serviceMonitor": {
                "enabled": True,
                "metricsInstance": {"enabled": False},
            },
            "selfMonitoring": {"enabled": False},
        },
    }

    release = kubernetes.helm.v3.Release(
        f"telemetry-archive-loki-{env}",
        kubernetes.helm.v3.ReleaseArgs(
            name=RELEASE_NAME,
            chart="loki",
            version=LOKI_CHART_VERSION,
            namespace=namespace,
            cleanup_on_fail=True,
            max_history=10,
            # The ingesters flush and the index gateway syncs before they
            # report ready, and three of them stand up in sequence.
            timeout=1200,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://grafana.github.io/helm-charts",
            ),
            values=values,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=depends_on),
    )
    return LokiRelease(
        release=release,
        distributor_service=f"{RELEASE_NAME}-distributor",
        query_frontend_service=f"{RELEASE_NAME}-query-frontend",
    )
