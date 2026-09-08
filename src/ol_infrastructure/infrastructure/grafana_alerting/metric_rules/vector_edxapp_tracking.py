"""edxapp tracking-log delivery alert rules, from the Vector sidecar's own metrics.

Source: mitodl/grafana-alerts#9 (cortex-rules/vector-edxapp-tracking.yaml), recreated
here rather than merged there -- grafana-alerts is being retired in favor of this
program (see the package AGENTS.md's Phase 5 note) and the Vector PodMonitor this
depends on (`k8s_resources.py`'s ``vector-metrics`` port, added in #4822) lives in
this repo already.

Why: tracking-log delivery to S3 stopped silently for mitxonline (7 days) and mitx
(15 days) in June 2026, with nothing alerting. ``EdxappTrackingLogSourceSilent``
would have caught the root cause (Vector receiving no events) within 3.5h;
``EdxappTrackingLogNoS3Delivery`` would have caught the end-to-end failure within
~4.25h (the sink's own batch timeout is 3600s).

Production only. All three rules filter to ``cluster=~".+-production"``, matching
the original -- non-prod tracking-log gaps are not the incident this responds to.

The `== 0`/silence trap
------------------------
Two of these three rules ask "did nothing happen" rather than "did something bad
happen", and that shape does not survive base.py's pipeline unchanged.
Stage C fires on ``last(A) > 0``, and a plain filtering comparison in PromQL
(``rate(...) == 0``) returns the matching series carrying its ORIGINAL value --
so a truly-silent source produces a row whose value is 0, and 0 > 0 never fires.
Same trap documented on ``DeploymentUnavailable*`` in eks_general.py,
``DagsterPgBouncerExporterDown`` in dagster_pgbouncer.py, and walked into directly
by an earlier revision of witan.py.

The fix used here is the same shape as ``DeploymentUnavailable*``: put a
naturally-positive left-hand side (a presence count of the metric at all) ahead of
an ``unless`` that strips it out whenever the real condition (some events/bytes
did flow) also holds. What survives is the left side's own value -- the series
count, always >= 1 -- rather than the filtered-through 0 a bare ``== 0`` would
leave behind.

``EdxappTrackingLogS3Error`` needs none of this: an error rate is already positive
exactly when the alert should fire, the same shape as witan.py's tool-call error
ratio.

Grouping
--------
``EdxappTrackingLogS3Error`` aggregates by ``component_id`` (two sinks match
``ship_edx_tracking_logs_to_s3.*`` -- the legacy and current pipelines, per
``applications/edxapp/files/vector/edxapp_tracking_log.yaml``), so
``component_id`` was added to ``NotificationPolicy.group_bies`` in
alertmanager.py; without it, both sinks firing at once in the same namespace
would collapse into one notification group. The other two rules aggregate by
``cluster, namespace`` only, both already in that list.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_PROD_CLUSTERS = ".+-production"


def _source_silent_expr() -> str:
    """No events received from the tracking-log file source in 3h.

    Left side: does the source exist at all in this cluster/namespace (Vector is
    up and configured with this source), independent of whether it received
    anything. Right side: has it received anything in the last 3h. ``unless``
    keeps the left side's row -- and its positive value -- exactly when the
    right side has no matching row, i.e. the received-event rate was 0
    throughout the window.
    """
    return (
        "count by (cluster, namespace) (\n"
        "  vector_component_received_events_total{\n"
        '    component_id="collect_edx_tracking_logs",\n'
        f'    cluster=~"{_PROD_CLUSTERS}"\n'
        "  }\n"
        ")\n"
        "unless\n"
        "  sum by (cluster, namespace) (\n"
        "    rate(vector_component_received_events_total{\n"
        '      component_id="collect_edx_tracking_logs",\n'
        f'      cluster=~"{_PROD_CLUSTERS}"\n'
        "    }[3h])\n"
        "  ) > 0"
    )


def _s3_error_expr() -> str:
    """S3 delivery errors on either tracking-log sink over 15m.

    Naturally positive when firing -- no ``unless`` flip needed, same shape as
    witan.py's tool-call error ratio.
    """
    return (
        "sum by (cluster, namespace, component_id) (\n"
        "  rate(vector_component_errors_total{\n"
        '    component_id=~"ship_edx_tracking_logs_to_s3.*",\n'
        f'    cluster=~"{_PROD_CLUSTERS}"\n'
        "  }[15m])\n"
        ") > 0"
    )


def _no_s3_delivery_expr() -> str:
    """No bytes delivered to S3 by the primary (non-legacy) sink in 4h.

    Same presence/``unless`` shape as ``_source_silent_expr`` -- 4h exceeds the
    sink's own 3600s batch timeout, so a healthy sink cannot go this long
    without a delivery.
    """
    return (
        "count by (cluster, namespace) (\n"
        "  vector_component_sent_bytes_total{\n"
        '    component_id="ship_edx_tracking_logs_to_s3",\n'
        f'    cluster=~"{_PROD_CLUSTERS}"\n'
        "  }\n"
        ")\n"
        "unless\n"
        "  sum by (cluster, namespace) (\n"
        "    rate(vector_component_sent_bytes_total{\n"
        '      component_id="ship_edx_tracking_logs_to_s3",\n'
        f'      cluster=~"{_PROD_CLUSTERS}"\n'
        "    }[4h])\n"
        "  ) > 0"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the edxapp tracking-log delivery alert rule group."""
    alerting.RuleGroup(
        "vector-edxapp-tracking",
        name="vector-edxapp-tracking",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="EdxappTrackingLogSourceSilent",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": (
                        "Vector in {{ $labels.namespace }} on {{ $labels.cluster }}"
                        " has received no events from the edxapp tracking log file"
                        " for 3 hours. The application may not be writing tracking"
                        " events to disk, or the Vector sidecar may be unhealthy."
                    ),
                },
                datas=rd(_source_silent_expr()),
            ),
            alerting.RuleGroupRuleArgs(
                name="EdxappTrackingLogS3Error",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": (
                        "Vector sink {{ $labels.component_id }} in"
                        " {{ $labels.namespace }} on {{ $labels.cluster }} is"
                        " reporting S3 delivery errors. Tracking logs may not be"
                        " reaching S3. Check Vector sidecar logs and verify IAM"
                        " role permissions on the edxapp-tracking S3 bucket."
                    ),
                },
                datas=rd(_s3_error_expr()),
            ),
            alerting.RuleGroupRuleArgs(
                name="EdxappTrackingLogNoS3Delivery",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "description": (
                        "No tracking log bytes have been delivered to S3 by Vector"
                        " in {{ $labels.namespace }} on {{ $labels.cluster }} for 4"
                        " hours (exceeds the 3600s batch timeout). Tracking events"
                        " are not reaching the edxapp-tracking S3 bucket. Check"
                        " Vector sidecar health and the tracking log file at"
                        " /openedx/data/logs/tracking_logs.log."
                    ),
                },
                datas=rd(_no_s3_delivery_expr()),
            ),
        ],
        opts=resource_opts,
    )
