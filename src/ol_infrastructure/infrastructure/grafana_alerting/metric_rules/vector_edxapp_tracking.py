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
The environment boundary is really the *stack* boundary rather than that regex:
this module is deployed to every stack, each querying its own Mimir tenant, so the
CI and QA stacks' own edxapp deployments are excluded by tenant before the cluster
filter is even consulted.

Three namespaces match today -- ``mitxonline-openedx`` in
``applications-production``, plus ``mitx-openedx`` and ``mitx-staging-openedx`` in
``residential-production``. That last one is in scope despite the name, and
deliberately so: ``mitx-staging`` is a peer deployment of ``mitx`` with its own
VPC and its own CI/QA/Production stacks
(``applications/edxapp/Pulumi.mitx-staging.Production.yaml``), serving residential
course authors who write courses as XML, and it ships tracking logs to a
production bucket of its own. Its firings are production signal and page like any
other -- do not add a namespace filter to exclude it. Same conclusion
apisix_edge.py reaches at the edge, under "Every host in the production stack is
production".

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

Lazy vs eager metric registration
----------------------------------
That presence count only holds up if the metric exists *while* the bad thing is
happening, and Vector is not consistent about this. It registers a component's
``component_received_events_total`` at 0 when it builds the topology -- verified
on ``ship_malformed_edx_tracking_logs_to_s3``, a sink that has never handled an
event and still reports 0 in all three production namespaces -- but it creates
``component_sent_bytes_total`` only on a component's first successful send, so
that same sink has no ``sent_bytes_total`` series at all.

So the presence side of both ``unless`` rules counts ``received_events_total``,
never ``sent_bytes_total`` -- including ``EdxappTrackingLogNoS3Delivery``, whose
``unless`` side measures sent bytes. Counting sent bytes for presence too would
make that rule self-defeating: a Vector process that has never delivered emits no
series at all, the expression evaluates to NoData, and ``no_data_state="OK"``
(mandatory here -- see base.py's docstring for why it cannot be Alerting) clears
it. An S3 outage outlasting a single pod roll -- an edxapp deploy, or one of the
Vector OOMKills noted in ``applications/edxapp/k8s_resources.py`` -- would
silently resolve the page and never re-fire, which is precisely the multi-day
incident this module exists to catch. Measured over 30 days of *healthy*
production, a sent_bytes presence count was already missing for 7-13 of 1440
half-hourly samples per namespace; the received_events presence count was present
for 1440 of 1440.

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

    The presence side counts the sink's ``received_events_total``, not the
    ``sent_bytes_total`` the ``unless`` side measures, because Vector only creates
    ``sent_bytes_total`` on a component's first successful send -- see the module
    docstring's "Lazy vs eager metric registration" for why counting it here would
    blind the rule exactly when it needs to fire.

    ``> 0`` on the presence side scopes the rule to what its name promises: events
    reached the sink but nothing left it. A sink that has been handed nothing at
    all is the source-silence case, which ``EdxappTrackingLogSourceSilent`` owns,
    and gating on it also keeps a freshly-created namespace from paging in the
    hour before its first batch flush. The comparison is inside ``count()``, so
    what reaches stage C is still the series count (always >= 1), not a
    filtered-through counter value.
    """
    return (
        "count by (cluster, namespace) (\n"
        "  vector_component_received_events_total{\n"
        '    component_id="ship_edx_tracking_logs_to_s3",\n'
        f'    cluster=~"{_PROD_CLUSTERS}"\n'
        "  } > 0\n"
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
