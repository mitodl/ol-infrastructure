"""Alert rules for the StarRocks lakehouse (namespace starrocks, data EKS).

Sources
-------
- FE and CN ``/metrics``, scraped by the ``starrocks-fe`` / ``starrocks-cn``
  ServiceMonitors in applications/starrocks. Both keep only an allowlist of
  families (``STARROCKS_FE_KEEP_METRICS`` / ``STARROCKS_CN_KEEP_METRICS``), so
  a rule on a family outside it evaluates to no data and, under
  no_data_state="OK", never fires. ``StarRocksFEMetricsMissing`` catches the
  case where a StarRocks upgrade renames ``node_info``, which most FE rules
  lean on.
- kube-state-metrics for the FE StatefulSet and the CN HPA.

Not duplicated here: ``StatefulSetReplicasMissing*`` and
``HPAAtMaxReplicas*`` in eks_general.py already cover a missing FE/CN pod and
the CN HPA pinned at its maximum.

FE quorum
---------
The FEs form a BDB-JE replication group. With fewer than a majority alive no
leader can be elected and every metadata write (DDL, load commits) stops, so
quorum loss is the one condition here that means StarRocks is down rather
than degraded. ``node_info{type="fe_node_num"}`` reports the group's alive and
total member counts from each FE's own view; ``max`` across FEs takes the most
complete one. That relies on a surviving FE still reporting the group's state
after quorum is lost, which has not been exercised here. If no FE is scraped
at all there is no ``node_info`` to read, so ``StarRocksFEAllDown`` covers that
case from kube-state-metrics.

Leader-only series
------------------
Compaction score and the ``lake_compaction_*`` / ``txn_*`` families are
populated only on the leader FE; followers report 0. ``max``/``sum`` across
FEs therefore reads the leader without needing to know which pod it is.

Values, not rows
----------------
base.py's pipeline fires on ``last(A) > 0``, so every expression here ends on
something positive when it should fire (see metric_rules/clickhouse.py).

Thresholds
----------
Compaction-score thresholds are StarRocks' own: ingestion is slowed above
``lake_ingest_slowdown_threshold`` (default 100) and the docs call a score
above 500 one that may need manual intervention
(docs/en/administration/management/compaction.md). The rest were set against
data-qa (about a day of history) and data-production (about 90 minutes) on
2026-10-15, where every signal but one sat at zero or well inside its
threshold: worst 15-minute internal-error share 1.8%, compaction score 10, FE
volumes under 6% used, CN memory under 30% of its limit, and no S3, publish,
vacuum or load-rejection failures at all. The exception is data-production's
FE membership: lakehouse-starrocks-fe-0 had been Pending for days, leaving 2
of 3 FEs alive, so StarRocksFENodeDown fires there from its first evaluation.

Severity and routing
--------------------
Every rule is Warning, in every environment, until Rootly routes
``service="starrocks"`` to its own service. The routing spec tiers StarRocks
at ``notify``, and a Critical here would page at Rootly's default High urgency
before that route exists. The first five rules (quorum lost, no FE scraped,
safe mode, FE volume full, compaction score past 500) are the ones that mean
StarRocks has stopped serving or loading, and are the candidates for a
production Critical once it does. Each Grafana stack only sees its own
environment's metrics, so an unfiltered rule is per environment already.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_ALL_CLUSTERS = ".+"
_LABELS = {"service": "starrocks"}
_FE = 'job="starrocks-fe"'
_CN = 'job="starrocks-cn"'
_BY_CLUSTER = "cluster, namespace"
_BY_POD = "cluster, namespace, pod"


def _fe_nodes(clusters: str, state: str) -> str:
    return (
        f"max by ({_BY_CLUSTER}) (node_info"
        f'{{{_FE}, cluster=~"{clusters}", type="fe_node_num", state="{state}"}})'
    )


def _fe_quorum_lost(clusters: str) -> str:
    # Dead members, firing once they reach ceil(total / 2): with 3 FEs that is
    # 2 dead, the point where the survivor cannot form a majority.
    total = _fe_nodes(clusters, "total")
    return (
        f"({total} - on ({_BY_CLUSTER}) {_fe_nodes(clusters, 'alive')})\n"
        f">= on ({_BY_CLUSTER}) ceil({total} / 2)"
    )


def _fe_all_down(clusters: str) -> str:
    return (
        f"max by ({_BY_CLUSTER}) (kube_statefulset_replicas"
        f'{{cluster=~"{clusters}", namespace="starrocks", '
        'statefulset=~".+-starrocks-fe"}) > 0\n'
        f"unless on ({_BY_CLUSTER})\n"
        f'(sum by ({_BY_CLUSTER}) (up{{{_FE}, cluster=~"{clusters}"}}) > 0)'
    )


def _safe_mode(clusters: str) -> str:
    return (
        f"max by ({_BY_CLUSTER}) (starrocks_fe_safe_mode"
        f'{{{_FE}, cluster=~"{clusters}"}}) > 0'
    )


def _fe_volume_full(clusters: str) -> str:
    # The meta volume holds the BDB-JE journal; an FE that cannot write it
    # stops. `pod` is lifted out of the PVC name because persistentvolumeclaim
    # is not in the notification policy's group_bies.
    selector = f'{{cluster=~"{clusters}", namespace="starrocks"}}'
    return (
        "label_replace(max by (cluster, namespace, persistentvolumeclaim) ("
        f"kubelet_volume_stats_used_bytes{selector}\n"
        f"/ kubelet_volume_stats_capacity_bytes{selector}), "
        '"pod", "$1", "persistentvolumeclaim", ".*-storage-(?:meta|log)-(.+)") > 0.9'
    )


def _compaction_score_very_high(clusters: str) -> str:
    return (
        f"max by ({_BY_CLUSTER}) (starrocks_fe_max_tablet_compaction_score"
        f'{{{_FE}, cluster=~"{clusters}"}}) > 500'
    )


def _warning(
    name: str,
    expr: str,
    for_: str,
    description: str,
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
) -> alerting.RuleGroupRuleArgs:
    return alerting.RuleGroupRuleArgs(
        name=name,
        condition="C",
        for_=for_,
        no_data_state="OK",
        exec_err_state="OK",
        labels={**_LABELS, "severity": "warning"},
        annotations={"description": description},
        datas=rd(expr),
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the StarRocks alert rule group."""
    alerting.RuleGroup(
        "starrocks",
        name="starrocks",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            # --- Not serving or not loading ---
            _warning(
                "StarRocksFEQuorumLost",
                _fe_quorum_lost(_ALL_CLUSTERS),
                "2m",
                "StarRocks in cluster {{ $labels.cluster }} has {{ $value }} FE members down, too few left for a majority. No leader can be elected, so DDL and load commits are stopped.",
                rd,
            ),
            _warning(
                "StarRocksFEAllDown",
                _fe_all_down(_ALL_CLUSTERS),
                "5m",
                "No StarRocks FE in cluster {{ $labels.cluster }} is being scraped. Either every FE is down or the starrocks-fe ServiceMonitor has stopped matching, and every other FE rule in this group is blind until it is back.",
                rd,
            ),
            _warning(
                "StarRocksSafeMode",
                _safe_mode(_ALL_CLUSTERS),
                "5m",
                "StarRocks in cluster {{ $labels.cluster }} is in safe mode: it rejects every load and aborts running transactions. The leader FE enters it when a backend disk falls below min(10%, 50GB) free.",
                rd,
            ),
            _warning(
                "StarRocksFEVolumeAlmostFull",
                _fe_volume_full(_ALL_CLUSTERS),
                "10m",
                "StarRocks FE volume {{ $labels.persistentvolumeclaim }} in cluster {{ $labels.cluster }} is {{ $value | humanizePercentage }} full. An FE that cannot write its metadata journal stops.",
                rd,
            ),
            _warning(
                "StarRocksCompactionScoreVeryHigh",
                _compaction_score_very_high(_ALL_CLUSTERS),
                "15m",
                "StarRocks in cluster {{ $labels.cluster }} has a partition at compaction score {{ $value }}. Loads into it are being slowed, StarRocks' docs say a score past 500 may need manual intervention, and at 2000 loads are rejected.",
                rd,
            ),
            # --- Degradation ---
            _warning(
                "StarRocksFENodeDown",
                f"{_fe_nodes('.+', 'total')} - on ({_BY_CLUSTER}) "
                f"{_fe_nodes('.+', 'alive')} > 0",
                "15m",
                "StarRocks in cluster {{ $labels.cluster }} has {{ $value }} FE member(s) down. The group still has a majority, but one more loss stops metadata writes.",
                rd,
            ),
            _warning(
                "StarRocksCNNotRegistered",
                f"max by ({_BY_CLUSTER}) (kube_horizontalpodautoscaler_status_current_replicas"
                '{namespace="starrocks"})\n'
                f"- on ({_BY_CLUSTER}) max by ({_BY_CLUSTER}) "
                f'(node_info{{{_FE}, type="cn_node_num", state="alive"}}) > 0',
                "15m",
                "StarRocks in cluster {{ $labels.cluster }} has {{ $value }} fewer CNs alive than the HPA is running. Those pods cost capacity but serve no queries.",
                rd,
            ),
            _warning(
                "StarRocksQueryInternalErrorRate",
                # Internal errors only: analysis errors are bad SQL and say
                # nothing about the engine. The traffic gate keeps one failed
                # query in a quiet window from reading as 100%.
                f"sum by ({_BY_CLUSTER}) (increase(starrocks_fe_query_internal_err{{{_FE}}}[15m]))\n"
                f"/ sum by ({_BY_CLUSTER}) (increase(starrocks_fe_query_total{{{_FE}}}[15m])) > 0.05\n"
                f"and on ({_BY_CLUSTER})\n"
                f"sum by ({_BY_CLUSTER}) (increase(starrocks_fe_query_total{{{_FE}}}[15m])) >= 20",
                "15m",
                "StarRocks in cluster {{ $labels.cluster }} failed {{ $value | humanizePercentage }} of queries with internal errors over the last 15 minutes.",
                rd,
            ),
            _warning(
                "StarRocksLoadsRejected",
                f"sum by ({_BY_CLUSTER}) (increase(starrocks_fe_txn_reject{{{_FE}}}[15m])) > 0",
                "0m",
                "StarRocks in cluster {{ $labels.cluster }} refused to begin {{ $value }} load transactions in the last 15 minutes, for a reused load label or the per-database running-transaction limit (max_running_txn_num_per_db).",
                rd,
            ),
            _warning(
                "StarRocksCompactionScoreHigh",
                f"max by ({_BY_CLUSTER}) (starrocks_fe_max_tablet_compaction_score{{{_FE}}}) > 100",
                "30m",
                "StarRocks in cluster {{ $labels.cluster }} has a partition at compaction score {{ $value }}, past the point where it slows loads (lake_ingest_slowdown_threshold, 100). Compaction is not keeping up with ingest.",
                rd,
            ),
            _warning(
                "StarRocksCompactionFailing",
                f"sum by ({_BY_CLUSTER}) (increase(starrocks_fe_lake_compaction_failed{{{_FE}}}[30m]))\n"
                f"+ on ({_BY_CLUSTER}) sum by ({_BY_CLUSTER}) "
                f"(increase(lake_aggregate_compaction_failed_tasks{{{_CN}}}[30m])) > 0",
                "15m",
                "StarRocks in cluster {{ $labels.cluster }} had {{ $value }} failed compaction tasks in the last 30 minutes. Scores climb while compaction fails; see information_schema.be_cloud_native_compactions for the error.",
                rd,
            ),
            _warning(
                "StarRocksSharedDataErrors",
                # Failures on the shared-data path to S3: throttling, shard
                # lookups, cache-file replication, version publish and vacuum.
                # Every one is zero at baseline in both environments.
                " +\n".join(
                    f"sum by ({_BY_CLUSTER}) (increase({metric}{{{_CN}}}[15m]))"
                    for metric in (
                        "fslib_qps_limit_error",
                        "starlet_get_shard_info_rpc_fail_count",
                        "fslib_file_replicate_fail_count",
                        "lake_publish_version_failed_tasks",
                        "lake_vacuum_del_file_fails",
                    )
                )
                + " > 0",
                "0m",
                "StarRocks CNs in cluster {{ $labels.cluster }} hit {{ $value }} shared-data failures (S3 throttling, shard lookup, cache replication, version publish or vacuum) in the last 15 minutes. The Storage Errors panel on the StarRocks - Overview dashboard splits them by kind.",
                rd,
            ),
            _warning(
                "StarRocksFEReplayLag",
                f"max by ({_BY_POD}) (starrocks_fe_meta_replay_lag_second{{{_FE}}}) > 300",
                "10m",
                "StarRocks follower FE {{ $labels.pod }} in cluster {{ $labels.cluster }} is {{ $value | humanizeDuration }} behind the leader's metadata, so queries it plans see stale catalog state.",
                rd,
            ),
            _warning(
                "StarRocksCNMemoryHigh",
                f"max by ({_BY_POD}) (starrocks_be_process_mem_bytes{{{_CN}}})\n"
                f"/ on ({_BY_POD}) max by ({_BY_POD}) (kube_pod_container_resource_limits"
                '{namespace="starrocks", container="cn", resource="memory"}) > 0.85',
                "10m",
                "StarRocks CN {{ $labels.pod }} in cluster {{ $labels.cluster }} is using {{ $value | humanizePercentage }} of its memory limit. At the limit the container is OOM-killed and its queries fail.",
                rd,
            ),
            _warning(
                "StarRocksScrapeDown",
                f'max by ({_BY_POD}) (1 - up{{job=~"starrocks-(fe|cn)"}}) > 0',
                "10m",
                "StarRocks pod {{ $labels.pod }} in cluster {{ $labels.cluster }} is not answering its metrics endpoint.",
                rd,
            ),
            _warning(
                # Guards the node_info-based rules above against a rename in a
                # StarRocks upgrade: they use no_data_state="OK" and would go
                # quiet rather than fail.
                "StarRocksFEMetricsMissing",
                f"max by ({_BY_POD}) (up{{{_FE}}} == 1)\n"
                f"unless on ({_BY_POD})\n"
                f'max by ({_BY_POD}) (node_info{{{_FE}, type="fe_node_num"}})',
                "15m",
                "StarRocks FE {{ $labels.pod }} in cluster {{ $labels.cluster }} is being scraped but reports no node_info. The FE quorum rules cannot fire until metric_rules/starrocks.py and the ServiceMonitor allowlist match the new metric names.",
                rd,
            ),
        ],
        opts=resource_opts,
    )
