"""Service-health overview for the StarRocks lakehouse (FE + CN, shared-data).

Every StarRocks panel reads a family kept by the ServiceMonitor allowlists in
applications/starrocks/__main__.py (STARROCKS_FE_KEEP_METRICS /
STARROCKS_CN_KEEP_METRICS); anything else is dropped before it reaches Mimir,
so a new panel on another family renders empty until the allowlist grows.
CPU, memory limits and replica counts come from cadvisor and
kube-state-metrics, since StarRocks' own CPU gauge reads the whole node.

Metric quirks this dashboard works around:

- Query latency is a per-FE Dropwizard summary (`quantile` label), so
  quantiles cannot be combined across FEs. Quantile panels show the worst FE
  (`max`). Its `_sum` is not a counter: it is reported as `_count` times the
  reservoir mean, so it falls whenever that mean does (prod: 31 resets in 6h
  on one FE, against none for `_count`). `rate(_sum) / rate(_count)` reads
  each fall as a counter reset and reported ~16s against a real ~1ms, so
  there is no mean-latency panel.
- An FE that stops receiving a catalog's queries keeps reporting that
  catalog's old quantiles: prod held an Iceberg p95 of 2136ms, unchanged,
  while its Iceberg `_count` stayed flat. Quantile panels therefore keep only
  FEs whose `_count` moved in the window.
- `starrocks_fe_query_latency_ms` is not split by catalog, so its quantiles
  are swamped by the default catalog's ~1ms statements; Iceberg latency is
  read from `starrocks_fe_catalog_query_latency_ms{catalog_type="iceberg"}`.
- The FE's `query_queue_*` families are typed `counter` but hold current
  levels, so they are read raw. The CN's `lake_*` failure families are typed
  `gauge` but are brpc `bvar::Adder`s that only grow, so they are read with
  `increase()`.
- `fslib_*_latency` histograms are in microseconds.
- `starrocks_be_pipe_scan_pending_tasks` is kept by the allowlist but is not
  charted: prod's six-day-old CNs report it at about -300, which no queue
  length can be, so it does not track anything usable.
- Leader-only state (compaction score, transactions, compaction tasks) is
  reported by every FE, but the followers report 0, so `max`/`sum` read the
  leader without needing its pod name.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

_FE = 'job="starrocks-fe"'
_CN = 'job="starrocks-cn"'
_NS = 'namespace="starrocks"'
_RATE = "[$__rate_interval]"


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "starrocks-overview",
        "title": "StarRocks - Overview",
        "description": (
            "Service health for the StarRocks lakehouse: FE quorum, query "
            "volume/errors/latency by catalog, CN capacity and data cache, "
            "and the shared-data S3 path."
        ),
        "tags": ["starrocks", "lakehouse"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "panels": [
            row_panel(title="Cluster health", y=0),
            stat_panel(
                title="FE Nodes Alive",
                expr=f'max(node_info{{{_FE}, type="fe_node_num", state="alive"}})',
                grid_pos={"h": 4, "w": 4, "x": 0, "y": 1},
            ),
            stat_panel(
                title="FE Nodes Expected",
                expr=f'max(node_info{{{_FE}, type="fe_node_num", state="total"}})',
                grid_pos={"h": 4, "w": 4, "x": 4, "y": 1},
            ),
            stat_panel(
                title="CN Nodes Alive",
                expr=f'max(node_info{{{_FE}, type="cn_node_num", state="alive"}})',
                grid_pos={"h": 4, "w": 4, "x": 8, "y": 1},
            ),
            stat_panel(
                title="Query Error % (selected range)",
                expr=(
                    f"sum(increase(starrocks_fe_query_err{{{_FE}}}[$__range]))\n/\n"
                    f"sum(increase(starrocks_fe_query_total{{{_FE}}}[$__range]))"
                ),
                grid_pos={"h": 4, "w": 4, "x": 12, "y": 1},
                unit="percentunit",
            ),
            stat_panel(
                title="Iceberg p95 Latency (worst FE)",
                expr=(
                    "max(starrocks_fe_catalog_query_latency_ms"
                    f'{{{_FE}, catalog_type="iceberg", quantile="0.95"}}'
                    "\nand on (pod) (increase(starrocks_fe_catalog_query_latency_ms_count"
                    f'{{{_FE}, catalog_type="iceberg"}}[$__range]) > 0))'
                ),
                grid_pos={"h": 4, "w": 4, "x": 16, "y": 1},
                unit="ms",
            ),
            stat_panel(
                title="FE Safe Mode",
                expr=f"max(starrocks_fe_safe_mode{{{_FE}}})",
                grid_pos={"h": 4, "w": 4, "x": 20, "y": 1},
            ),
            row_panel(title="Queries", y=5),
            timeseries_panel(
                title="Queries/s by Catalog",
                expr=(
                    "sum by (catalog_type) (rate(starrocks_fe_catalog_query_total"
                    f"{{{_FE}}}{_RATE}))"
                ),
                legend_format="{{catalog_type}}",
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 6},
                unit="reqps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Query Errors/s by Kind",
                description=(
                    "`err` counts every failed query; `analysis` (bad SQL, "
                    "missing table) and `internal` (engine faults) are subsets "
                    "of it. Internal errors are the ones worth chasing."
                ),
                queries=[
                    {
                        "expr": f"sum(rate(starrocks_fe_query_err{{{_FE}}}{_RATE}))",
                        "legend_format": "all errors",
                    },
                    {
                        "expr": (
                            "sum(rate(starrocks_fe_query_analysis_err"
                            f"{{{_FE}}}{_RATE}))"
                        ),
                        "legend_format": "analysis",
                    },
                    {
                        "expr": (
                            "sum(rate(starrocks_fe_query_internal_err"
                            f"{{{_FE}}}{_RATE}))"
                        ),
                        "legend_format": "internal",
                    },
                    {
                        "expr": (
                            f"sum(rate(starrocks_fe_query_timeout{{{_FE}}}{_RATE}))"
                        ),
                        "legend_format": "timeout",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 6},
                unit="reqps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Query Latency p95 by Catalog (worst FE)",
                description=(
                    "Highest p95 across FEs, per catalog. FEs that served no "
                    "query for a catalog in the step are left out, because an "
                    "idle reservoir keeps reporting stale values."
                ),
                expr=(
                    "max by (catalog_type) (starrocks_fe_catalog_query_latency_ms"
                    f'{{{_FE}, quantile="0.95"}}'
                    "\nand on (pod, catalog_type) "
                    "(increase(starrocks_fe_catalog_query_latency_ms_count"
                    f"{{{_FE}}}{_RATE}) > 0))"
                ),
                legend_format="{{catalog_type}}",
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 14},
                unit="ms",
                legend_calc="max",
            ),
            timeseries_panel(
                title="Iceberg Query Latency Quantiles (worst FE)",
                description=(
                    "Each FE reports its own reservoir quantiles, which cannot "
                    "be merged; this shows the highest across FEs. An FE that "
                    "served no Iceberg query in the step is left out, because "
                    "an idle reservoir keeps reporting stale values. Empty "
                    "means no Iceberg queries."
                ),
                expr=(
                    "max by (quantile) (starrocks_fe_catalog_query_latency_ms"
                    f'{{{_FE}, catalog_type="iceberg", quantile=~"0.75|0.95|0.99"}}'
                    "\nand on (pod) (increase(starrocks_fe_catalog_query_latency_ms_count"
                    f'{{{_FE}, catalog_type="iceberg"}}{_RATE}) > 0))'
                ),
                legend_format="p{{quantile}}",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 14},
                unit="ms",
                legend_calc="max",
            ),
            timeseries_panel(
                title="Query Queue",
                queries=[
                    {
                        "expr": f"sum(starrocks_fe_query_queue_pending{{{_FE}}})",
                        "legend_format": "pending",
                    },
                    {
                        "expr": (
                            f"sum(starrocks_fe_query_queue_slot_running{{{_FE}}})"
                        ),
                        "legend_format": "slots running",
                    },
                    {
                        "expr": (
                            f"sum(starrocks_fe_query_queue_slot_pending{{{_FE}}})"
                        ),
                        "legend_format": "slots pending",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 22},
                legend_calc="max",
            ),
            timeseries_panel(
                title="Client Connections by FE",
                expr=f"sum by (pod) (starrocks_fe_connection_total{{{_FE}}})",
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 22},
                legend_calc="max",
            ),
            timeseries_panel(
                title="Slow Queries & Rejected Thrift Connections/s",
                queries=[
                    {
                        "expr": f"sum(rate(starrocks_fe_slow_query{{{_FE}}}{_RATE}))",
                        "legend_format": "slow queries",
                    },
                    {
                        "expr": (
                            "sum(rate(starrocks_fe_thrift_server_rejected_connections_total"
                            f"{{{_FE}}}{_RATE}))"
                        ),
                        "legend_format": "rejected connections",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 22},
                unit="reqps",
                legend_calc="mean",
            ),
            row_panel(title="Frontend (FE)", y=30),
            timeseries_panel(
                title="FE JVM Heap Used",
                queries=[
                    {
                        "expr": (
                            f'sum by (pod) (jvm_heap_size_bytes{{{_FE}, type="used"}})'
                        ),
                        "legend_format": "{{pod}}",
                    },
                    {
                        "expr": f'max(jvm_heap_size_bytes{{{_FE}, type="max"}})',
                        "legend_format": "max heap",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 31},
                unit="bytes",
                legend_calc="max",
            ),
            timeseries_panel(
                title="FE CPU (cores)",
                expr=(
                    "sum by (pod) (rate(container_cpu_usage_seconds_total"
                    f'{{{_NS}, container="fe"}}{_RATE}))'
                ),
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 31},
                legend_calc="mean",
            ),
            timeseries_panel(
                title="FE JVM Threads",
                expr=f'sum by (pod) (jvm_thread{{{_FE}, type="count"}})',
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 31},
                legend_calc="max",
            ),
            timeseries_panel(
                title="Follower Metadata Replay Lag",
                description=(
                    "How far each follower's metadata image trails the leader. "
                    "A follower that keeps falling behind serves stale catalog "
                    "state."
                ),
                expr=f"max by (pod) (starrocks_fe_meta_replay_lag_second{{{_FE}}})",
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 39},
                unit="s",
                legend_calc="max",
            ),
            timeseries_panel(
                title="FE Volume Usage",
                expr=(
                    "max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes"
                    f"{{{_NS}}}\n/\nkubelet_volume_stats_capacity_bytes{{{_NS}}})"
                ),
                legend_format="{{persistentvolumeclaim}}",
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 39},
                unit="percentunit",
                legend_calc="max",
            ),
            timeseries_panel(
                title="Load Transactions/s (leader)",
                queries=[
                    {
                        "expr": f"sum(rate(starrocks_fe_txn_success{{{_FE}}}{_RATE}))",
                        "legend_format": "success",
                    },
                    {
                        "expr": f"sum(rate(starrocks_fe_txn_failed{{{_FE}}}{_RATE}))",
                        "legend_format": "failed",
                    },
                    {
                        "expr": f"sum(rate(starrocks_fe_txn_reject{{{_FE}}}{_RATE}))",
                        "legend_format": "rejected",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 39},
                unit="reqps",
                legend_calc="mean",
            ),
            row_panel(title="Compute nodes (CN)", y=47),
            timeseries_panel(
                title="CN Replicas (HPA)",
                queries=[
                    {
                        "expr": (
                            "max(kube_horizontalpodautoscaler_status_current_replicas"
                            f"{{{_NS}}})"
                        ),
                        "legend_format": "current",
                    },
                    {
                        "expr": (
                            "max(kube_horizontalpodautoscaler_status_desired_replicas"
                            f"{{{_NS}}})"
                        ),
                        "legend_format": "desired",
                    },
                    {
                        "expr": (
                            "max(kube_horizontalpodautoscaler_spec_max_replicas"
                            f"{{{_NS}}})"
                        ),
                        "legend_format": "max",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 48},
                decimals=0,
                legend_calc="max",
            ),
            timeseries_panel(
                title="CN CPU (cores)",
                queries=[
                    {
                        "expr": (
                            "sum by (pod) (rate(container_cpu_usage_seconds_total"
                            f'{{{_NS}, container="cn"}}{_RATE}))'
                        ),
                        "legend_format": "{{pod}}",
                    },
                    {
                        "expr": (
                            "max(kube_pod_container_resource_limits"
                            f'{{{_NS}, container="cn", resource="cpu"}})'
                        ),
                        "legend_format": "limit",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 48},
                legend_calc="mean",
            ),
            timeseries_panel(
                title="CN Process Memory",
                queries=[
                    {
                        "expr": (
                            f"sum by (pod) (starrocks_be_process_mem_bytes{{{_CN}}})"
                        ),
                        "legend_format": "{{pod}}",
                    },
                    {
                        "expr": (
                            "max(kube_pod_container_resource_limits"
                            f'{{{_NS}, container="cn", resource="memory"}})'
                        ),
                        "legend_format": "limit",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 48},
                unit="bytes",
                legend_calc="max",
            ),
            timeseries_panel(
                title="CN Memory by Consumer",
                queries=[
                    {
                        "expr": f"sum(starrocks_be_{kind}_mem_bytes{{{_CN}}})",
                        "legend_format": kind,
                    }
                    for kind in ("query", "load", "compaction", "metadata")
                ],
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 56},
                unit="bytes",
                legend_calc="max",
            ),
            timeseries_panel(
                title="Data Cache Hit Ratio",
                description=(
                    "Share of block reads served from the CN's local data cache "
                    "instead of S3. A drop after a scale-out is expected while "
                    "new CNs warm up."
                ),
                expr=(
                    "sum(rate(starrocks_be_block_cache_hit_count"
                    f"{{{_CN}}}{_RATE}))\n/\n"
                    "(sum(rate(starrocks_be_block_cache_hit_count"
                    f"{{{_CN}}}{_RATE}))"
                    " + sum(rate(starrocks_be_block_cache_miss_count"
                    f"{{{_CN}}}{_RATE})))"
                ),
                legend_format="hit ratio",
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 56},
                unit="percentunit",
            ),
            timeseries_panel(
                title="Data Cache Disk Usage",
                expr=(
                    "sum by (pod) (starrocks_be_datacache_disk_used_bytes"
                    f"{{{_CN}}})\n/\n"
                    "sum by (pod) (starrocks_be_datacache_disk_quota_bytes"
                    f"{{{_CN}}})"
                ),
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 56},
                unit="percentunit",
                legend_calc="max",
            ),
            timeseries_panel(
                title="Bytes Scanned/s by CN",
                expr=f"sum by (pod) (rate(starrocks_be_query_scan_bytes{{{_CN}}}{_RATE}))",
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 64},
                unit="Bps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Query Spill Written/s",
                description=(
                    "Queries spilling intermediate state to local disk because "
                    "they outgrew their memory budget."
                ),
                expr=(
                    "sum by (pod) (rate(starrocks_be_query_spill_bytes_write_total"
                    f"{{{_CN}}}{_RATE}))"
                ),
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 64},
                unit="Bps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Pipeline Driver Queue",
                expr=f"sum by (pod) (starrocks_be_pipe_driver_queue_len{{{_CN}}})",
                legend_format="{{pod}}",
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 64},
                legend_calc="max",
            ),
            row_panel(title="Shared-data storage (S3)", y=72),
            timeseries_panel(
                title="Read Latency p99 by Filesystem",
                description=(
                    "Split by the filesystem layer StarRocks reports the read "
                    "against: `s3`, `cachefs` (the data-cache layer) or "
                    "`srposix` (local files)."
                ),
                expr=(
                    "histogram_quantile(0.99, sum by (le, fstype) "
                    f"(rate(fslib_read_io_latency_bucket{{{_CN}}}{_RATE})))"
                ),
                legend_format="{{fstype}}",
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 73},
                unit="µs",
                legend_calc="max",
            ),
            timeseries_panel(
                title="S3 Upload Latency p99",
                queries=[
                    {
                        "expr": (
                            "histogram_quantile(0.99, sum by (le) "
                            f"(rate(fslib_s3_{kind}_upload_latency_bucket"
                            f"{{{_CN}}}{_RATE})))"
                        ),
                        "legend_format": f"{kind}-part",
                    }
                    for kind in ("single", "multi")
                ],
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 73},
                unit="µs",
                legend_calc="max",
            ),
            timeseries_panel(
                title="I/O Throughput by Filesystem",
                queries=[
                    {
                        "expr": (
                            "sum by (fstype) (rate(fslib_read_io_size_sum"
                            f"{{{_CN}}}{_RATE}))"
                        ),
                        "legend_format": "read {{fstype}}",
                    },
                    {
                        "expr": (
                            "sum by (fstype) (rate(fslib_write_io_size_sum"
                            f"{{{_CN}}}{_RATE}))"
                        ),
                        "legend_format": "write {{fstype}}",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 73},
                unit="Bps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Storage Errors (per interval)",
                description=(
                    "Each series is a count of failures in the graph interval. "
                    "Any sustained non-zero line here means the CN is struggling "
                    "to read, write or clean up shared-data files in S3."
                ),
                queries=[
                    {
                        "expr": (
                            f"sum(increase(fslib_qps_limit_error{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "S3 throttled",
                    },
                    {
                        "expr": (
                            "sum(increase(fslib_file_replicate_fail_count"
                            f"{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "file replicate failed",
                    },
                    {
                        "expr": (
                            "sum(increase(starlet_get_shard_info_rpc_fail_count"
                            f"{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "shard info RPC failed",
                    },
                    {
                        "expr": (
                            "sum(increase(lake_vacuum_del_file_fails"
                            f"{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "vacuum delete failed",
                    },
                    {
                        "expr": (
                            "sum(increase(lake_publish_version_failed_tasks"
                            f"{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "publish version failed",
                    },
                ],
                grid_pos={"h": 8, "w": 24, "x": 0, "y": 81},
                decimals=0,
            ),
            row_panel(title="Compaction & materialized views", y=89),
            timeseries_panel(
                title="Max Tablet Compaction Score (leader)",
                description=(
                    "Highest pending-compaction score across all partitions. "
                    "A score that keeps climbing means ingest is outrunning "
                    "compaction."
                ),
                expr=f"max(starrocks_fe_max_tablet_compaction_score{{{_FE}}})",
                legend_format="score",
                grid_pos={"h": 8, "w": 8, "x": 0, "y": 90},
                legend_calc="max",
            ),
            timeseries_panel(
                title="Compaction Tasks (per interval)",
                queries=[
                    {
                        "expr": (
                            "sum(increase(starrocks_fe_lake_compaction_success"
                            f"{{{_FE}}}[$__interval]))"
                        ),
                        "legend_format": "succeeded",
                    },
                    {
                        "expr": (
                            "sum(increase(starrocks_fe_lake_compaction_failed"
                            f"{{{_FE}}}[$__interval]))"
                        ),
                        "legend_format": "failed",
                    },
                    {
                        "expr": (
                            "sum(increase(lake_aggregate_compaction_failed_tasks"
                            f"{{{_CN}}}[$__interval]))"
                        ),
                        "legend_format": "failed on CN",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 8, "y": 90},
                decimals=0,
            ),
            timeseries_panel(
                title="MV Refresh Jobs (per interval)",
                queries=[
                    {
                        "expr": (
                            "sum(increase(starrocks_fe_mv_global_refresh_jobs_total"
                            f"{{{_FE}}}[$__interval]))"
                        ),
                        "legend_format": "started",
                    },
                    {
                        "expr": (
                            "sum(increase(starrocks_fe_mv_global_refresh_failed_jobs_total"
                            f"{{{_FE}}}[$__interval]))"
                        ),
                        "legend_format": "failed",
                    },
                ],
                grid_pos={"h": 8, "w": 8, "x": 16, "y": 90},
                decimals=0,
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the StarRocks service-health overview dashboard."""
    create_dashboard(
        "starrocks-overview-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, stat_panel, row_panel),
        resource_opts,
    )
