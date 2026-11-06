"""The Mimir release: the archive's metrics backend (plan D6).

`mimir-distributed` chart in the classic architecture: distributors push to
ingesters directly, with no Kafka between them. Replication factor 1 on the
single-zone archive node group, blocks and ruler state on the archive bucket
under distinct prefixes through IRSA, compactor-enforced retention, OTLP
ingest beside remote write, and the ruler on so the long-range recording rules
(recording_rules.py) are evaluated where the data is. QA runs the same code as
a 30-day canary at the smallest counts that still exercise every component
(plan D2).
"""

from dataclasses import dataclass

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from bridge.lib.versions import MIMIR_DISTRIBUTED_CHART_VERSION
from ol_infrastructure.applications.observability_archive.storage import (
    MIMIR_BLOCKS_STORAGE_PREFIX,
    MIMIR_RULER_STORAGE_PREFIX,
)
from ol_infrastructure.lib.aws.eks_helper import ecr_dockerhub_registry
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    AWSBase,
    Services,
    cluster_addon_labels,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

RELEASE_NAME = "mimir"
HTTP_PORT = 8080
# Pinned explicitly rather than inherited from the chart's appVersion so a
# chart bump and a Mimir bump are separate, reviewable diffs.
MIMIR_IMAGE_TAG = "3.2.3"
STORAGE_CLASS = "ebs-gp3-sc"

# What Grafana Cloud promotes from OTLP resource attributes to metric labels,
# so a query written against Cloud's series (service_red.py groups by
# k8s_pod_name, otel_service_red.py by service_name) reads the same labels
# here. Mimir's default promotes nothing beyond job and instance.
OTEL_PROMOTED_RESOURCE_ATTRIBUTES = [
    "service.instance.id",
    "service.name",
    "service.namespace",
    "service.version",
    "cloud.availability_zone",
    "cloud.region",
    "container.name",
    "deployment.environment",
    "deployment.environment.name",
    "k8s.cluster.name",
    "k8s.container.name",
    "k8s.cronjob.name",
    "k8s.daemonset.name",
    "k8s.deployment.name",
    "k8s.job.name",
    "k8s.namespace.name",
    "k8s.pod.name",
    "k8s.replicaset.name",
    "k8s.statefulset.name",
]

# Mimir's own series that reach Grafana Cloud through the ServiceMonitors.
# Unfiltered, each Mimir pod exposes a few thousand series, most of them
# per-route request histograms and Go runtime detail; Loki's unfiltered
# ServiceMonitors already put ~12k series into the QA stack. This keeps what
# the archive alerts (metric_rules/telemetry_archive.py) and a health
# dashboard read: throughput, drops, series counts, compaction and rule
# evaluation health, object-store errors, memory, and the request totals
# without their buckets.
SELF_METRICS_KEEP_REGEX = "|".join(  # noqa: FLY002
    [
        "cortex_build_info",
        "cortex_ring_members",
        "cortex_distributor_(received_samples|samples_in|received_requests)_total",
        "cortex_discarded_samples_total",
        "cortex_ingester_(memory|active|owned)_series",
        "cortex_ingester_ingested_samples_total",
        "cortex_ingester_memory_series_(created|removed)_total",
        "cortex_ingester_tsdb_(head_max_timestamp_seconds|out_of_order_samples_appended_total)",
        "cortex_ingester_tsdb_(compactions_failed|wal_writes_failed|wal_corruptions)_total",
        "cortex_ingester_shipper_(uploads|upload_failures)_total",
        "cortex_ingester_oldest_unshipped_block_timestamp_seconds",
        "cortex_compactor_(last_successful_run_timestamp_seconds|runs_(started|completed|failed)_total)",
        "cortex_compactor_(blocks_cleaned|blocks_marked_for_deletion|block_cleanup_failures)_total",
        "cortex_compactor_disk_out_of_space_errors_total",
        "cortex_bucket_(blocks_count|index_last_successful_update_timestamp_seconds)",
        "cortex_bucket_store_(blocks_loaded|block_loads_total|block_load_failures_total)",
        "cortex_bucket_stores_blocks_last_successful_sync_timestamp_seconds",
        "cortex_prometheus_rule_(evaluation_failures|evaluations|group_iterations_missed)_total",
        "cortex_prometheus_rule_group_(rules|last_evaluation_timestamp_seconds)",
        "cortex_ruler_(queries|queries_failed|write_requests|write_requests_failed)_total",
        "cortex_ruler_managers_total",
        "cortex_query_frontend_queries_total",
        "cortex_query_frontend_queue_length",
        "cortex_request_duration_seconds_(count|sum)",
        "cortex_runtime_config_last_reload_successful",
        "thanos_objstore_bucket_(operations|operation_failures)_total",
        "go_memstats_heap_inuse_bytes",
        "process_(resident_memory_bytes|cpu_seconds_total)",
        "memcached_(up|current_connections|items_evicted_total|commands_total|limit_bytes|current_bytes)",
        "up",
    ]
)


@dataclass(frozen=True)
class MimirSizing:
    """Replica counts, resources and limits for one environment."""

    ingesters: int
    distributors: int
    queriers: int
    store_gateways: int
    ingester_cpu_request: str
    querier_cpu_request: str
    compactor_cpu_request: str
    ingester_memory_request: str
    ingester_memory_limit: str
    ingester_volume: str
    store_gateway_memory_request: str
    store_gateway_memory_limit: str
    store_gateway_volume: str
    compactor_memory_request: str
    compactor_memory_limit: str
    compactor_volume: str
    querier_memory_request: str
    querier_memory_limit: str
    results_cache_mb: int
    metadata_cache_mb: int
    max_global_series: int
    ingestion_rate: int
    ingestion_burst_size: int


# Production carries ~416k active series at ~8.1k samples/s (ADR-0014); QA's
# 225k series are over half of that. At RF1 each ingester holds a third of the
# series. Mimir's guidance is 1 core and 2.5 GB per 300k in-memory series plus
# 50% headroom, which these requests follow with the limits as the headroom.
SIZING: dict[str, MimirSizing] = {
    "qa": MimirSizing(
        ingesters=3,
        distributors=2,
        queriers=2,
        store_gateways=1,
        ingester_cpu_request="250m",
        querier_cpu_request="250m",
        compactor_cpu_request="250m",
        ingester_memory_request="1.5Gi",
        ingester_memory_limit="3Gi",
        ingester_volume="20Gi",
        store_gateway_memory_request="1Gi",
        store_gateway_memory_limit="2Gi",
        store_gateway_volume="20Gi",
        compactor_memory_request="1Gi",
        compactor_memory_limit="3Gi",
        compactor_volume="50Gi",
        querier_memory_request="1Gi",
        querier_memory_limit="2Gi",
        results_cache_mb=128,
        metadata_cache_mb=128,
        max_global_series=600_000,
        ingestion_rate=30_000,
        ingestion_burst_size=300_000,
    ),
    "production": MimirSizing(
        ingesters=3,
        distributors=2,
        queriers=2,
        store_gateways=3,
        ingester_cpu_request="1",
        querier_cpu_request="500m",
        compactor_cpu_request="500m",
        ingester_memory_request="2.5Gi",
        ingester_memory_limit="5Gi",
        ingester_volume="50Gi",
        store_gateway_memory_request="1.5Gi",
        store_gateway_memory_limit="3Gi",
        store_gateway_volume="30Gi",
        compactor_memory_request="2Gi",
        compactor_memory_limit="6Gi",
        compactor_volume="100Gi",
        querier_memory_request="1.5Gi",
        querier_memory_limit="3Gi",
        results_cache_mb=256,
        metadata_cache_mb=256,
        max_global_series=1_000_000,
        ingestion_rate=50_000,
        ingestion_burst_size=500_000,
    ),
}


@dataclass
class MimirRelease:
    """What the ingress routes, the rule sync and the Cloud datasource point at."""

    release: kubernetes.helm.v3.Release
    distributor_service: str
    query_frontend_service: str
    ruler_service: str
    http_port: int = HTTP_PORT


def build(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    aws_config: AWSBase,
    k8s_global_labels: dict[str, str],
    namespace: str,
    cluster_name: Output[str],
    bucket_name: str,
    irsa_role_arn: Output[str],
    service_account: str,
    retention_days: int,
    node_selector: dict[str, str],
    tolerations: list[dict[str, str]],
    depends_on: list[Resource],
) -> MimirRelease:
    """Install Mimir in the classic architecture on the archive node group."""
    env = stack_info.env_suffix
    sizing = SIZING[env]
    registry = ecr_dockerhub_registry()

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
            **extra,
        }

    def resources(cpu: str, memory: str, limit: str) -> dict[str, object]:
        return {
            "requests": {"cpu": cpu, "memory": memory},
            "limits": {"memory": limit},
        }

    def volume(size: str) -> dict[str, object]:
        return {"enabled": True, "size": size, "storageClass": STORAGE_CLASS}

    def cache(allocated_mb: int) -> dict[str, object]:
        # The chart derives a 500m CPU request from nothing when resources are
        # unset, which is most of a QA node's spare CPU for a cache this size.
        return placement(
            enabled=True,
            replicas=1,
            allocatedMemory=allocated_mb,
            resources=resources(
                "50m", f"{int(allocated_mb * 1.2)}Mi", f"{allocated_mb * 2}Mi"
            ),
        )

    values: dict[str, object] = {
        "image": {"repository": f"{registry}/grafana/mimir", "tag": MIMIR_IMAGE_TAG},
        "global": {"podLabels": pod_labels},
        "serviceAccount": {
            "create": True,
            "name": service_account,
            "annotations": {"eks.amazonaws.com/role-arn": irsa_role_arn},
        },
        # Classic architecture (plan D6). The chart's default is Kafka-backed
        # ingest storage with a bundled single-node Kafka, a third stateful
        # system the ADR never sized. Each setting its classic-architecture.yaml
        # nulls out is set explicitly here instead, so the result does not
        # depend on how the chart's config merge treats a null.
        "kafka": {"enabled": False},
        "mimir": {
            "structuredConfig": {
                # Writers and the PDC read path bypass any gateway, so nothing
                # would set X-Scope-OrgID; one tenant it is.
                "multitenancy_enabled": False,
                "usage_stats": {"enabled": False},
                "ingest_storage": {"enabled": False},
                "distributor": {"remote_timeout": "2s"},
                "ingester": {
                    "push_grpc_method_enabled": True,
                    "ring": {"replication_factor": 1},
                },
                "store_gateway": {"sharding_ring": {"replication_factor": 1}},
                "common": {
                    "storage": {
                        "backend": "s3",
                        "s3": {
                            "endpoint": f"s3.{aws_config.region}.amazonaws.com",
                            "bucket_name": bucket_name,
                            "region": aws_config.region,
                        },
                    }
                },
                # One bucket, two prefixes: Mimir refuses to start when blocks
                # and ruler state share a bucket and prefix, and the lifecycle
                # backstop in storage.py expires only the blocks prefix.
                "blocks_storage": {"storage_prefix": MIMIR_BLOCKS_STORAGE_PREFIX},
                "ruler_storage": {"storage_prefix": MIMIR_RULER_STORAGE_PREFIX},
                "ruler": {
                    # No alertmanager (plan D13: nothing alerts from the
                    # archive). Empty disables notification discovery; the
                    # chart's default points at an alertmanager Service that
                    # does not exist here.
                    "alertmanager_url": "",
                },
                "limits": {
                    "compactor_blocks_retention_period": f"{retention_days}d",
                    "max_global_series_per_user": sizing.max_global_series,
                    "ingestion_rate": sizing.ingestion_rate,
                    "ingestion_burst_size": sizing.ingestion_burst_size,
                    # OTLP series carry more labels than scraped ones once the
                    # resource attributes below are promoted.
                    "max_label_names_per_series": 60,
                    # A writer that drains its queue after an archive outage
                    # replays samples up to sample_age_limit (1h) old, in
                    # order per series but possibly older than the head
                    # block's start after a 2h head compaction.
                    "out_of_order_time_window": "2h",
                    # Both OTLP translation settings are per-tenant limits in
                    # Mimir 3.x even though their flags keep the distributor.
                    # prefix; Mimir 3.2.3 rejects them under `distributor`.
                    # The attribute list is a comma-separated string, not a
                    # YAML list. Grafana Cloud's OTLP gateway names the series
                    # http_server_duration_milliseconds_bucket; without the
                    # suffixes Mimir would store http_server_duration_bucket
                    # and every query written against Cloud would miss here.
                    "otel_metric_suffixes_enabled": True,
                    "promote_otel_resource_attributes": ",".join(
                        OTEL_PROMOTED_RESOURCE_ATTRIBUTES
                    ),
                },
            }
        },
        # Replication factor 1 on one zone (plan D3): the chart's default of
        # three zone-aware StatefulSets per ring would schedule two of them
        # nowhere, and the rollout operator exists to drive them.
        "rollout_operator": {"enabled": False},
        "ingester": placement(
            replicas=sizing.ingesters,
            zoneAwareReplication={"enabled": False},
            persistentVolume=volume(sizing.ingester_volume),
            resources=resources(
                sizing.ingester_cpu_request,
                sizing.ingester_memory_request,
                sizing.ingester_memory_limit,
            ),
        ),
        "store_gateway": placement(
            replicas=sizing.store_gateways,
            zoneAwareReplication={"enabled": False},
            persistentVolume=volume(sizing.store_gateway_volume),
            resources=resources(
                "250m",
                sizing.store_gateway_memory_request,
                sizing.store_gateway_memory_limit,
            ),
        ),
        "compactor": placement(
            replicas=1,
            persistentVolume=volume(sizing.compactor_volume),
            resources=resources(
                sizing.compactor_cpu_request,
                sizing.compactor_memory_request,
                sizing.compactor_memory_limit,
            ),
        ),
        "distributor": placement(
            replicas=sizing.distributors,
            resources=resources("250m", "512Mi", "1.5Gi"),
        ),
        "querier": placement(
            replicas=sizing.queriers,
            resources=resources(
                sizing.querier_cpu_request,
                sizing.querier_memory_request,
                sizing.querier_memory_limit,
            ),
        ),
        "query_frontend": placement(
            replicas=1,
            resources=resources("250m", "512Mi", "1.5Gi"),
        ),
        "query_scheduler": placement(
            replicas=1,
            resources=resources("100m", "128Mi", "512Mi"),
        ),
        "ruler": placement(
            replicas=1,
            # The chart's default 1 GiB ballast is sized for rulers evaluating
            # thousands of rules.
            memBallastSizeBytes=128 * 1024 * 1024,
            resources=resources("250m", "512Mi", "1.5Gi"),
        ),
        # The results cache saves re-reading months of blocks for a repeated
        # dashboard load; the metadata cache saves S3 LIST and GET calls from
        # every store-gateway, querier and ruler bucket sync, which is the
        # one line of the archive's bill that scales with queries. Chunk and
        # index caches stay off at this query volume.
        "results-cache": cache(sizing.results_cache_mb),
        "metadata-cache": cache(sizing.metadata_cache_mb),
        "chunks-cache": {"enabled": False},
        "index-cache": {"enabled": False},
        "memcached": {"image": {"repository": f"{registry}/library/memcached"}},
        "memcachedExporter": {
            "image": {"repository": f"{registry}/prom/memcached-exporter"}
        },
        # None of the optional components: no alertmanager and no overrides
        # exporter (nothing alerts from the archive and there is one tenant),
        # no nginx gateway (writers come through the archive's Traefik and the
        # PDC agent reads the query-frontend Service directly), no MinIO, no
        # continuous test.
        "alertmanager": {"enabled": False},
        "overrides_exporter": {"enabled": False},
        "gateway": {"enabled": False},
        "minio": {"enabled": False},
        "continuous_test": {"enabled": False},
        # The operations cluster's prometheusOperatorObjects feature picks the
        # ServiceMonitors up and ships Mimir's own metrics to Grafana Cloud,
        # filtered to the families the alerts and a health dashboard read.
        # The chart's clusterLabel defaults to the release name, which would
        # hide these series from every cluster-scoped query.
        "metaMonitoring": {
            "dashboards": {"enabled": False},
            "prometheusRule": {"enabled": False},
            "grafanaAgent": {"enabled": False},
            "serviceMonitor": {
                "enabled": True,
                "clusterLabel": cluster_name,
                "metricRelabelings": [
                    {
                        "action": "keep",
                        "sourceLabels": ["__name__"],
                        "regex": SELF_METRICS_KEEP_REGEX,
                    }
                ],
            },
        },
    }

    release = kubernetes.helm.v3.Release(
        f"telemetry-archive-mimir-{env}",
        kubernetes.helm.v3.ReleaseArgs(
            name=RELEASE_NAME,
            chart="mimir-distributed",
            version=MIMIR_DISTRIBUTED_CHART_VERSION,
            namespace=namespace,
            cleanup_on_fail=True,
            max_history=10,
            # Ingesters replay their WAL and store-gateways sync the bucket
            # index before reporting ready, in sequence per StatefulSet.
            timeout=1200,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://grafana.github.io/helm-charts",
            ),
            values=values,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=depends_on),
    )
    return MimirRelease(
        release=release,
        distributor_service=f"{RELEASE_NAME}-distributor",
        query_frontend_service=f"{RELEASE_NAME}-query-frontend",
        ruler_service=f"{RELEASE_NAME}-ruler",
    )
