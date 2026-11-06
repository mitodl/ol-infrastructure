"""Alerts on the telemetry archive (ADR-0014) dropping, aging or going unread.

The archive is best effort by design: a stalled archive must never hold up the
Grafana Cloud stream that shares each writer, so the writers drop the archive's
copy instead of waiting. These rules are how a drop that lasts becomes known.
Everything here is ticket tier (ADR-0014 step 6, plan D13): the archive never
pages, and nothing in the archive is ever a datasource for a rule.

What is covered, and what is not
--------------------------------
Writers with a scrape: the k8s-monitoring collectors (every cluster) through
the chart's Alloy integration, and vector-log-proxy through its PodMonitor.
The EC2 hosts running Vector (Concourse, Consul, Vault) and Alloy (Concourse
web) have no scrape of their own: Alloy's self-report keeps only alloy_build_*
and a few prometheus_* series, and Vector's internal_metrics is not enabled
there. Their drops are visible only in journald on the host.

Metric names were confirmed against what the collectors emit before the rules
landed; what reaches Cloud is checked in the QA soak of each phase.
`otelcol_exporter_send_failed_log_records_total` and the
`prometheus_remote_storage_samples_(dropped|failed)_total` pair are in the
chart's default Alloy allowlist; `otelcol_exporter_enqueue_failed_log_records_total`
is added to includeMetrics in substructure/aws/eks/grafana.py because it is
what a full non-blocking queue increments. Mimir's own series arrive through
its ServiceMonitors, filtered to the families named in
observability_archive/mimir.py.

Routing
-------
`channel=devops-warnings` with `severity=warning`: Slack, no Rootly, same as
alloy_pipeline.py. Nobody is woken for an archive gap; someone looks within the
day.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_ROUTING = {"channel": "devops-warnings"}
_NAMESPACE = "observability-archive"
_ONE_DAY_SECONDS = 24 * 60 * 60

# Every k8s Alloy pod's metrics reach Mimir twice (see alloy_pipeline.py);
# collector_id="" keeps the cluster scrape, which names the cluster. The two
# counters are selected together rather than added: each exists only after
# its own kind of failure, and send_failed carries labels enqueue_failed
# lacks, so `a + b` would be empty whenever only one side is failing.
_ALLOY_DROPS_EXPR = (
    "sum by (cluster, namespace, pod, exporter) ("
    "rate({__name__=~"
    '"otelcol_exporter_(send|enqueue)_failed_log_records_total",'
    ' exporter=~".*archive.*", collector_id=""}[10m])'
    ") > 0"
)
# Vector counts a drop by its non-blocking buffer (`when_full: drop_newest`)
# under buffer_discarded_events_total and a drop by the sink itself (retries
# exhausted) under component_discarded_events_total; a slow archive shows only
# in the first, a refused write only in the second.
_VECTOR_DROPS_EXPR = (
    "sum by (cluster, namespace, pod, component_id) ("
    "rate({__name__=~"
    '"vector_(buffer|component)_discarded_events_total",'
    ' component_id=~"archive_.*"}[10m])'
    ") > 0"
)
# A compactor that has started but not applied retention for a day. The
# timestamp is 0 until the first run, which the second clause excludes.
_RETENTION_STALE_EXPR = (
    "(time() - max by (cluster, namespace) ("
    "loki_compactor_apply_retention_last_successful_run_timestamp_seconds"
    f'{{namespace="{_NAMESPACE}"}}))'
    f" > {_ONE_DAY_SECONDS}"
    " and max by (cluster, namespace) ("
    "loki_compactor_apply_retention_last_successful_run_timestamp_seconds"
    f'{{namespace="{_NAMESPACE}"}}) > 0'
)
# The archive remote write on each alloy-metrics collector (Phase 2). dropped
# is what sample_age_limit discards after a long outage and failed is what the
# endpoint refused; selected together for the same reason as the OTLP pair
# above. The Cloud remote write shares the pod but not the url.
_REMOTE_WRITE_DROPS_EXPR = (
    "sum by (cluster, namespace, pod, url) ("
    "rate({__name__=~"
    '"prometheus_remote_storage_samples_(dropped|failed)_total",'
    ' url=~".*telemetry-archive.*", collector_id=""}[10m])'
    ") > 0"
)
# Mimir's compactor: same shape as the Loki rule. Mimir runs compaction every
# 30 minutes and a run also applies blocks retention.
_MIMIR_COMPACTION_STALE_EXPR = (
    "(time() - max by (cluster, namespace) ("
    "cortex_compactor_last_successful_run_timestamp_seconds"
    f'{{namespace="{_NAMESPACE}"}}))'
    f" > {_ONE_DAY_SECONDS}"
    " and max by (cluster, namespace) ("
    "cortex_compactor_last_successful_run_timestamp_seconds"
    f'{{namespace="{_NAMESPACE}"}}) > 0'
)
# The ruler evaluating the archive's recording rules. A failing group means a
# 13-month trend has a gap that cannot be backfilled.
_RULER_FAILURES_EXPR = (
    "sum by (cluster, namespace, rule_group) ("
    "rate(cortex_prometheus_rule_evaluation_failures_total"
    f'{{namespace="{_NAMESPACE}"}}[10m])'
    ") > 0"
)
# Two agents is the floor for a rolling restart not breaking queries (plan D8).
# `bool` because rd()'s threshold stage fires on value > 0: a plain `< 2`
# returns the replica count itself, which is 0 when both agents are down.
_PDC_AGENTS_EXPR = (
    "min by (cluster, namespace, deployment) ("
    "kube_deployment_status_replicas_available"
    f'{{namespace="{_NAMESPACE}", deployment="pdc-agent"}}) < bool 2'
)


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the telemetry archive rule group."""
    alerting.RuleGroup(
        "telemetry-archive",
        name="telemetry-archive",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveAlloyDroppingLogs",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Alloy {{ $labels.pod }} on {{ $labels.cluster }} is dropping archive logs",
                    "description": "The archive exporter {{ $labels.exporter }} in {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.cluster }} has failed to send or enqueue log records for 30 minutes. Grafana Cloud delivery is unaffected by design; the archive is missing these lines. Check the archive write endpoint (docs/telemetry-archive-runbook.md) and the Loki distributors in observability-archive on the operations cluster.",
                },
                datas=rd(_ALLOY_DROPS_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveVectorDroppingLogs",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "vector-log-proxy {{ $labels.pod }} is dropping archive logs",
                    "description": "Sink {{ $labels.component_id }} in {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.cluster }} has discarded events for 30 minutes: its bounded buffer is full or the archive is refusing writes. The Grafana Cloud sinks are unaffected. See docs/telemetry-archive-runbook.md.",
                },
                datas=rd(_VECTOR_DROPS_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveRetentionNotApplied",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Loki compactor on {{ $labels.cluster }} has not applied retention in 24 hours",
                    "description": "The archive's compactor in {{ $labels.namespace }} on {{ $labels.cluster }} last applied retention more than a day ago. Data past retention is not being removed and delete requests are not being processed; the S3 lifecycle rule is the only backstop. Check the loki-compactor pod and its S3 access. See docs/telemetry-archive-runbook.md.",
                },
                datas=rd(_RETENTION_STALE_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveAlloyDroppingMetrics",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Alloy {{ $labels.pod }} on {{ $labels.cluster }} is dropping archive metrics",
                    "description": "The archive remote write in {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.cluster }} has dropped or failed samples for 30 minutes. Grafana Cloud delivery is unaffected by design; the archive is missing these samples. Check the archive write endpoint (docs/telemetry-archive-runbook.md) and the Mimir distributors in observability-archive on the operations cluster.",
                },
                datas=rd(_REMOTE_WRITE_DROPS_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveMimirCompactionStale",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Mimir compactor on {{ $labels.cluster }} has not completed a run in 24 hours",
                    "description": "The archive's Mimir compactor in {{ $labels.namespace }} on {{ $labels.cluster }} last completed a run more than a day ago. Blocks are not being compacted and blocks retention is not being applied; queries get slower and the S3 lifecycle rule is the only backstop. Check the mimir-compactor pod, its volume and its S3 access. See docs/telemetry-archive-runbook.md.",
                },
                datas=rd(_MIMIR_COMPACTION_STALE_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchiveRulerEvaluationFailing",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Mimir ruler on {{ $labels.cluster }} is failing to evaluate {{ $labels.rule_group }}",
                    "description": "Rule group {{ $labels.rule_group }} in the archive's Mimir ruler ({{ $labels.namespace }} on {{ $labels.cluster }}) has failed evaluations for 30 minutes, so the recorded 13-month trend it produces has a gap. Check the mimir-ruler pod's logs for the failing expression; the rules are declared in observability_archive/recording_rules.py. See docs/telemetry-archive-runbook.md.",
                },
                datas=rd(_RULER_FAILURES_EXPR),
            ),
            alerting.RuleGroupRuleArgs(
                name="TelemetryArchivePdcAgentsDegraded",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", **_ROUTING},
                annotations={
                    "summary": "Fewer than 2 PDC agents available on {{ $labels.cluster }}",
                    "description": "The pdc-agent Deployment in {{ $labels.namespace }} on {{ $labels.cluster }} has had fewer than two available replicas for 15 minutes. Grafana Cloud reads the archive through these agents; with one left a restart breaks queries, with none the archive datasources fail. See docs/telemetry-archive-runbook.md.",
                },
                datas=rd(_PDC_AGENTS_EXPR),
            ),
        ],
        opts=resource_opts,
    )
