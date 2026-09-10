"""Alert rules for the shared LLMOps ClickHouse cluster (namespace clickhouse).

Sources
-------
Two scrape targets feed these rules, and they name things differently.

- The Altinity operator's metrics exporter, ``chi_clickhouse_*`` (ServiceMonitor
  in substructure/aws/eks/clickhouse_operator.py). It queries every CHI host's
  system tables and labels each series with ``chi``, ``hostname`` and, through
  honorLabels, ``namespace="clickhouse"``. Every exporter metric used here was
  read off the live exporter in data-production on 2026-09-10.
- Keeper's own Prometheus endpoint, ``ClickHouse*_Keeper*`` (ServiceMonitor in
  applications/clickhouse). Keeper has no SQL interface for the exporter to
  query. These names come from Altinity's prometheus-alert-rules-chkeeper.yaml
  at release-0.26.0 and could not be read live before the endpoint existed:
  confirm them on the first environment this reaches.

Series that only exist once non-zero
------------------------------------
The exporter emits ``chi_clickhouse_event_*`` and ``DetachedParts`` only after
the underlying counter first moves. None of RejectedInserts, the DelayedInserts
event or DetachedParts existed in production on 2026-09-10. ``increase()``
needs two samples, so it cannot see the first appearance of such a series, and
for rejected inserts the first appearance is the incident. That rule carries a
second arm (``x unless x offset 10m``) for that case. DelayedInserts uses the
``chi_clickhouse_metric_`` gauge (always present) instead of the event.

Values, not rows
----------------
base.py's pipeline fires on ``last(A) > 0``, so every expression here ends on
something positive when it should fire (see metric_rules/witan.py). That is
why target-down rules use ``1 - up`` rather than ``up == 0``, and the disk
prediction negates ``predict_linear`` rather than testing ``< 0``.

Severity and routing
--------------------
Conditions that mean data is at risk or writes are failing get a
production-only Critical rule plus a Warning for CI/QA. The rest are Warning
everywhere; each Grafana stack only sees its own environment's metrics, so an
unfiltered rule is per environment already. Every rule carries
``service="clickhouse"``, which the Grafana Production Service Route in
saas/rootly matches to the ``LLMOps - ClickHouse`` service.

Thresholds are Altinity's defaults except where a comment says otherwise.
Production on 2026-09-10 sat well inside all of them: 11 parts in the fullest
partition (threshold 100), zero replication delay, one Keeper session per
replica, 1.59 of 1.61 TB free per replica.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"
_LABELS = {"service": "clickhouse"}
_BY_HOST = "cluster, namespace, hostname"
_BY_KEEPER = "cluster, namespace, pod"
_KEEPER_JOB = 'namespace="clickhouse", job="clickhouse-keeper-metrics"'

# The backup CronJob runs daily (applications/clickhouse, clickhouse-backup).
# 26h is one missed run plus two hours of slack.
_BACKUP_STALE_SECONDS = 26 * 3600


def _server_down(clusters: str) -> str:
    # The exporter records a fetch error per host per query type; system.metrics
    # is the first query it makes, so an error there means the host did not
    # answer at all.
    return (
        f"max by ({_BY_HOST}) (chi_clickhouse_metric_fetch_errors"
        f'{{cluster=~"{clusters}", fetch_type="system.metrics"}}) > 0'
    )


def _readonly_replica(clusters: str) -> str:
    return (
        f"max by ({_BY_HOST}) (chi_clickhouse_metric_ReadonlyReplica"
        f'{{cluster=~"{clusters}"}}) > 0'
    )


def _rejected_inserts(clusters: str) -> str:
    selector = f'{{cluster=~"{clusters}"}}'
    return (
        f"sum by ({_BY_HOST}) (increase(chi_clickhouse_event_RejectedInserts"
        f"{selector}[10m])) > 0\n"
        "or\n"
        f"sum by ({_BY_HOST}) (chi_clickhouse_event_RejectedInserts{selector}\n"
        f"  unless chi_clickhouse_event_RejectedInserts{selector} offset 10m) > 0"
    )


def _disk_almost_full(clusters: str) -> str:
    # Used fraction, not free fraction: a disk with zero bytes free would give
    # a free fraction of 0, which the pipeline's `> 0` threshold never fires on.
    return (
        f"max by ({_BY_HOST}) (1 - chi_clickhouse_metric_DiskFreeBytes"
        f'{{cluster=~"{clusters}", disk="default"}}'
        f" / chi_clickhouse_metric_DiskTotalBytes"
        f'{{cluster=~"{clusters}", disk="default"}}) > 0.9'
    )


def _keeper_lost_quorum() -> str:
    # Production only, and not paired with a CI/QA rule: CI and QA run a single
    # Keeper, which is a leader with zero synced followers by construction and
    # would satisfy this permanently. Add QA once its canary ensemble has three
    # members.
    return (
        f"max by ({_BY_KEEPER}) (ClickHouseAsyncMetrics_KeeperIsLeader"
        f'{{cluster=~"{_PROD_CLUSTERS}"}}) == 1\n'
        f"and on ({_BY_KEEPER})\n"
        f"max by ({_BY_KEEPER}) (ClickHouseAsyncMetrics_KeeperSyncedFollowers"
        f'{{cluster=~"{_PROD_CLUSTERS}"}}) < 1'
    )


def _backup_stale(clusters: str) -> str:
    return (
        "max by (cluster, namespace, cronjob) (time() - "
        "(kube_cronjob_status_last_successful_time"
        f'{{cluster=~"{clusters}", namespace="clickhouse", '
        f'cronjob="clickhouse-backup"}} > 0)) > {_BACKUP_STALE_SECONDS}'
    )


def _backup_never_succeeded() -> str:
    # eks_general.py's staleness rules cannot see a CronJob that has never
    # succeeded (kube-state-metrics omits last_successful_time until the first
    # success). Same shape as witan.py's _never_succeeded_expr: the age term
    # leads so that its positive value is what reaches the threshold.
    selector = 'namespace="clickhouse", cronjob="clickhouse-backup"'
    return (
        "(\n"
        "  (\n"
        "    time() - max by (cluster, namespace, cronjob) (\n"
        f"      kube_cronjob_created{{{selector}}}\n"
        f"    ) > {_BACKUP_STALE_SECONDS}\n"
        "  )\n"
        "  and on (cluster, namespace, cronjob) (\n"
        "    max by (cluster, namespace, cronjob) (\n"
        f"      kube_cronjob_spec_suspend{{{selector}}}\n"
        "    ) == 0\n"
        "  )\n"
        ")\n"
        "unless on (cluster, namespace, cronjob)\n"
        "  max by (cluster, namespace, cronjob) (\n"
        f"    kube_cronjob_status_last_successful_time{{{selector}}}\n"
        "  )"
    )


def _paired(
    name: str,
    expr: Callable[[str], str],
    for_: str,
    description: str,
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
) -> list[alerting.RuleGroupRuleArgs]:
    """Build a Warning (CI/QA) and Critical (production) pair of one rule."""
    return [
        alerting.RuleGroupRuleArgs(
            name=f"{name}Warning",
            condition="C",
            for_=for_,
            no_data_state="OK",
            exec_err_state="OK",
            labels={**_LABELS, "severity": "warning"},
            annotations={"description": description},
            datas=rd(expr(_NON_PROD_CLUSTERS)),
        ),
        alerting.RuleGroupRuleArgs(
            name=f"{name}Critical",
            condition="C",
            for_=for_,
            no_data_state="OK",
            exec_err_state="KeepLast",
            labels={**_LABELS, "severity": "critical"},
            annotations={"description": description},
            datas=rd(expr(_PROD_CLUSTERS)),
        ),
    ]


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
    """Create the ClickHouse alert rule group."""
    alerting.RuleGroup(
        "clickhouse",
        name="clickhouse",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            # --- Data at risk or writes failing ---
            *_paired(
                "ClickHouseServerDown",
                _server_down,
                "5m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} is not answering the operator's metrics exporter.",
                rd,
            ),
            *_paired(
                "ClickHouseReadonlyReplica",
                _readonly_replica,
                "5m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} has {{ $value }} replicated tables in read-only mode, usually a lost Keeper session. Inserts routed to it fail.",
                rd,
            ),
            *_paired(
                "ClickHouseRejectedInserts",
                _rejected_inserts,
                "0m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} rejected INSERTs for too many parts in a partition. Opik's SDK drops batches after retries, so these are lost traces.",
                rd,
            ),
            *_paired(
                "ClickHouseDiskAlmostFull",
                _disk_almost_full,
                "10m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} has used {{ $value | humanizePercentage }} of its data volume.",
                rd,
            ),
            *_paired(
                "ClickHouseBackupStale",
                _backup_stale,
                "15m",
                "The clickhouse-backup CronJob in cluster {{ $labels.cluster }} has not succeeded in over 26 hours. AWS Backup EBS snapshots are the only recovery layer until it does.",
                rd,
            ),
            alerting.RuleGroupRuleArgs(
                name="ClickHouseKeeperLostQuorumCritical",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={**_LABELS, "severity": "critical"},
                annotations={
                    "description": "ClickHouse Keeper leader {{ $labels.pod }} in cluster {{ $labels.cluster }} has no synced followers. Keeper cannot commit, so every replicated table is read-only."
                },
                datas=rd(_keeper_lost_quorum()),
            ),
            # --- Degradation ---
            _warning(
                "ClickHouseBackupNeverSucceeded",
                _backup_never_succeeded(),
                "15m",
                "The clickhouse-backup CronJob in cluster {{ $labels.cluster }} was created {{ $value | humanizeDuration }} ago and has never succeeded.",
                rd,
            ),
            _warning(
                "ClickHouseReplicationDelay",
                f"max by ({_BY_HOST}) (chi_clickhouse_metric_ReplicasMaxAbsoluteDelay) > 300",
                "10m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} is {{ $value | humanizeDuration }} behind its replicas.",
                rd,
            ),
            _warning(
                "ClickHouseDelayedInserts",
                f"max by ({_BY_HOST}) (chi_clickhouse_metric_DelayedInserts) > 0",
                "10m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} is throttling INSERTs for too many active parts. Rejection follows if merges do not catch up.",
                rd,
            ),
            _warning(
                "ClickHousePartsPerPartitionHigh",
                f"max by ({_BY_HOST}) (chi_clickhouse_metric_MaxPartCountForPartition) > 100",
                "15m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} has {{ $value }} parts in one partition. Merges are falling behind inserts.",
                rd,
            ),
            _warning(
                "ClickHouseKeeperSessions",
                f"max by ({_BY_HOST}) (chi_clickhouse_metric_ZooKeeperSession) > 1",
                "5m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} holds {{ $value }} Keeper sessions; more than one risks stale reads.",
                rd,
            ),
            _warning(
                "ClickHouseDiskFillPredicted",
                # Altinity predicts 24h ahead; 3 days leaves time to resize an
                # EBS volume during working hours.
                f"max by ({_BY_HOST}) (-1 * predict_linear(chi_clickhouse_metric_DiskFreeBytes"
                '{disk="default"}[1d], 3 * 86400)) > 0',
                "30m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} will run out of disk within 3 days at the current rate.",
                rd,
            ),
            _warning(
                "ClickHouseDetachedParts",
                f"sum by ({_BY_HOST}) (chi_clickhouse_metric_DetachedParts) > 0",
                "30m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} has {{ $value }} detached parts. Check system.detached_parts for the reason.",
                rd,
            ),
            _warning(
                # The cold_s3 disk reports 16 EiB free, so a disk-space rule
                # means nothing there. A missing object behind a part that
                # ClickHouse still references does, and it is data loss.
                "ClickHouseColdTierMissingObjects",
                f"sum by ({_BY_HOST}) (increase(chi_clickhouse_metric_DiskS3NoSuchKeyErrors[15m])) > 0",
                "0m",
                "ClickHouse host {{ $labels.hostname }} in cluster {{ $labels.cluster }} hit S3 NoSuchKey errors on the cold tier: a part references an object that is gone.",
                rd,
            ),
            _warning(
                "ClickHouseMetricsExporterDown",
                'max by (cluster) (1 - up{namespace="kube-system", job="clickhouse-operator-metrics"}) > 0',
                "10m",
                "The ClickHouse operator's metrics exporter in cluster {{ $labels.cluster }} is not being scraped. Every rule in this group except the Keeper and backup rules goes blind.",
                rd,
            ),
            _warning(
                "ClickHouseKeeperDown",
                f"max by ({_BY_KEEPER}) (1 - up{{{_KEEPER_JOB}}}) > 0",
                "5m",
                "ClickHouse Keeper pod {{ $labels.pod }} in cluster {{ $labels.cluster }} is not answering its metrics endpoint.",
                rd,
            ),
            _warning(
                "ClickHouseKeeperOutstandingRequests",
                f"max by ({_BY_KEEPER}) (ClickHouseMetrics_KeeperOutstandingRequests) > 10",
                "10m",
                "ClickHouse Keeper pod {{ $labels.pod }} in cluster {{ $labels.cluster }} has {{ $value }} requests queued; it is receiving more than it can process.",
                rd,
            ),
        ],
        opts=resource_opts,
    )
