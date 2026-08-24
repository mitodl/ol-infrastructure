"""Alert rules for the shared witan MCP service and the omnigraph store behind it.

Written 2026-08-24. witan is deployed as a shared multi-user service
(docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md) and until now
had telemetry but no alerting: applications/witan/observability.py wires OTel,
structlog and Sentry into the pods, and nothing reads any of it on a schedule.

What is already covered elsewhere, and deliberately not repeated here
----------------------------------------------------------------------
eks_general.py's ``WorkloadJobFailed*`` already covers a witan or omnigraph Job
reaching the Failed condition, and its ``ScheduledJobStaleFast*`` already covers
witan-token-sync. Both deliberately exclude witan-ci-indexer, which fails on
nearly every run for a known reason
(tk-witan-ci-indexer-cronjob-fails-on-nearly-every-r-4c1462); nothing here
re-adds paging for it, since the exclusions have to be lifted together with the
fix rather than routed around.

What this module adds is the part that is specific to witan's own failure
shapes rather than to it being a Kubernetes workload.

WitanToolCallErrorRate: the brownout detector
-----------------------------------------------
omnigraph runs with quarantine semantics -- OMNIGRAPH_REQUIRE_ALL_GRAPHS is
deliberately unset, so a graph that fails to open is skipped rather than fatal.
That is the right blast radius now that the cluster serves ``council``,
``code-bridge`` and one ``code-<repo>`` graph per managed repo: all-or-nothing
would let a rebuildable per-repo code graph take down the memory graph every
agent depends on. The residual risk is narrow and severe: if ``council``
specifically fails to open, the pod stays Ready, /healthz returns 200, and
every real request 404s.

Neither health endpoint can close that. omnigraph's ``/healthz`` is flat by
design, and witan's answers from process state alone on purpose -- a probe that
checks the data tier converts backend SLOWNESS into frontend DEATH, which is
exactly what took the service down on 2026-08-12 when ToolHive's proxy health
check pinged its saturated backend and the kubelet killed a container that was
working. ``/graphs`` cannot substitute either: it is gated behind a
``graph_list`` grant on ``Server::"root"``, so no unauthenticated prober reaches
it.

So the signal has to come from real traffic, and the tool-call outcome counter
is a good one. witan cannot serve any tool call without ``council``, so losing
it drives the error ratio to ~1.0 across every tool at once.

★ Threshold sized against the measured baseline, not guessed. Over the 7 days
to 2026-08-24 the hourly error ratio on production-witan was 0 in almost every
bucket, with excursions to 0.013 and 0.050 and a single peak of 0.143. 0.5 sits
well clear of that and far below the ~1.0 a lost graph produces.

★ THIS IS TRAFFIC-DEPENDENT, AND THAT IS A REAL GAP, NOT AN OVERSIGHT. With no
calls in the window the ratio is 0/0 = NaN, which never satisfies the
comparison, so a graph lost during a quiet period is not reported until someone
tries to use it. The boot-time half of the signal is in
log_rules/witan.py::WitanGraphQuarantined, which is not traffic-dependent but
only has something to say when a pod starts. Closing the remaining window needs
an authenticated synthetic probe that asserts ``council`` is queryable; that is
tracked separately and is not something an alert rule can do.

WitanDailyMaintenanceStale: the daily bucket eks_general.py does not have
---------------------------------------------------------------------------
eks_general.py's staleness rules come in two buckets, fast (6h) and slow (15d),
because PromQL cannot parse a cron expression to derive a per-job threshold.
Neither fits a DAILY job: 6h fires every day, and 15d lets one stop for two
weeks. 36 hours is one missed run plus half a day. Two members, both daily:

  omnigraph-optimize   20 3 * * *   nightly Lance compaction. Sits in the slow
    bucket today, so it can stop for two weeks while nothing fails: every
    commit adds fragments, reads degrade by roughly 21x on an uncompacted
    store, and the degradation is gradual and silent. This overlaps
    ScheduledJobStaleSlow* rather than replacing it, deliberately -- that rule
    is still the right backstop for the other jobs in its bucket, and editing
    its membership would change alerting for cms-edxapp-reindex-courses too.

  witan-view-reaper    50 4 * * *   ADR-0006's stale-view reaper, "the only
    process ever entitled to delete a WIP view". In no bucket at all: it was
    deployed on 2026-08-24 (#5573) and is live in CI and QA with Production
    promotion still in flight. If it stops, nothing else bounds code-graph
    branch sprawl, and the sprawl is invisible -- which is the closest
    practical signal to the "code-graph branch count/growth over time" the
    shared-service observability ask named. Confirmed present in the QA Mimir
    tenant with schedule `50 4 * * *` before adding it here.

Its `> 0` guard means the rule stays silent for a CronJob that has not yet had
a first success, which is exactly the state witan-view-reaper is in as this
ships. That case is WitanScheduledJobNeverSucceeded's, and its 10-day age gate
correctly keeps it quiet for a CronJob created today.

WitanScheduledJobNeverSucceeded: the gap eks_general.py documents
------------------------------------------------------------------
Every staleness rule is blind to a CronJob that has NEVER succeeded, because
kube-state-metrics omits ``kube_cronjob_status_last_successful_time`` entirely
until the first success -- an absent series is NoData, and no_data_state=OK
keeps it silent. eks_general.py names this gap and leaves it open because a
general version would page immediately for two pre-existing open-metadata
CronJobs and for witan-break-glass.

This one is scoped to the witan and omnigraph namespaces, which excludes the
open-metadata pair by construction, and filters on ``spec_suspend == 0``, which
excludes witan-break-glass properly rather than by name -- it is suspended and
scheduled for a February 31st that never arrives, and either fact alone should
keep it out. The 10-day age gate clears the longest cadence in these namespaces
(weekly), so a newly created CronJob is not reported before it has had a real
chance to run.

Verification
--------------
Every expression here was evaluated against the production Mimir tenant on
2026-08-24 before being committed, and each was also shown to be able to FIRE
rather than only to be quiet -- an expression that returns nothing is
indistinguishable from one that is broken:

  WitanScheduledJobNeverSucceeded  returns nothing as committed; relaxing the
                                   ``spec_suspend == 0`` filter to ``>= 0``
                                   returns exactly witan-break-glass, which
                                   proves the ``unless`` join, the age gate and
                                   the label matching all work.
  WitanDailyMaintenanceStale       returned 47,818s for omnigraph-optimize
                                   against its 129,600s threshold when the
                                   threshold was dropped to 1 (last success
                                   13.3h earlier).
  WitanToolCallErrorRate           see the baseline figures above.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"

# witan_tool_calls_total carries outcome ∈ {ok, error, input_required}.
# `input_required` is a normal elicitation, not a failure: the observability
# middleware is registered OUTSIDE MRTRElicitationMiddleware precisely so
# "the server asked the user a question" is not counted as an error. It lands
# in the denominator and not the numerator, which is correct.
_ERROR_RATIO_THRESHOLD = 0.5

# One missed nightly run plus half a day.
_DAILY_STALE_SECONDS = 129600

# Every daily-cadence maintenance CronJob across the witan and omnigraph
# namespaces. Validate a new entry against its `schedule` label on
# kube_cronjob_info before adding it, the same discipline eks_general.py's
# bucket membership asks for.
_DAILY_MAINTENANCE_CRONJOBS = "omnigraph-optimize|witan-view-reaper"

# Longer than the longest cadence in these namespaces (weekly), so a newly
# created CronJob is not reported before it could plausibly have succeeded.
_NEVER_SUCCEEDED_MIN_AGE_SECONDS = 864000

_WITAN_NAMESPACES = "witan|omnigraph"

_ERROR_RATE_SUMMARY = (
    "Over half of witan's MCP tool calls in {{ $labels.cluster }} are failing"
)

_ERROR_RATE_DESCRIPTION = (
    "witan in cluster {{ $labels.cluster }} is failing more than half of its MCP"
    " tool calls. A ratio near 1.0 with the pod still Ready is the signature of a"
    " quarantined `council` graph: omnigraph skips a graph it cannot open rather"
    " than refusing to start, so the process serves, /healthz returns 200, and"
    " every request against the missing graph 404s. Check the omnigraph-server"
    " boot line for the graph count it actually opened (`kubectl -n omnigraph"
    " logs deploy/omnigraph-server | grep 'serving omnigraph'`) against what"
    " cluster.yaml declares -- WitanGraphQuarantined in log_rules/witan.py"
    " watches that same number. Then read witan's own mcp.tool_call lines"
    " (`kubectl -n witan logs deploy/witan-server`), which carry tool, outcome,"
    " duration_ms and actor_id, to see whether the failures are one tool or all"
    " of them. All of them means the graph; one of them means that tool."
)

_DAILY_STALE_SUMMARY = (
    "Daily maintenance job {{ $labels.cronjob }} has not succeeded in"
    " {{ $labels.cluster }} for over 36 hours"
)

_DAILY_STALE_DESCRIPTION = (
    "The daily maintenance CronJob {{ $labels.cronjob }} in namespace"
    " {{ $labels.namespace }}, cluster {{ $labels.cluster }}, has not completed"
    " successfully for over 36 hours. Neither of these jobs fails loudly when it"
    " stops -- that is why they need a window sized to their cadence rather than"
    " eks_general.py's 15-day slow bucket."
    " omnigraph-optimize is Lance compaction: every commit adds fragments, reads"
    " degrade by roughly 21x on an uncompacted store, and the store keeps serving"
    " while it gets slower. Its sweep is per-graph and continues past a failure,"
    " so its final log lines name exactly which graphs did not compact."
    " witan-view-reaper is the only process entitled to delete a stale WIP"
    " code-graph view (ADR-0006); if it stops, nothing bounds branch sprawl and"
    " nothing else reports it. Start with `kubectl -n {{ $labels.namespace }} get"
    " jobs` and read the most recent run for this CronJob."
)

_NEVER_SUCCEEDED_SUMMARY = (
    "CronJob {{ $labels.cronjob }} in {{ $labels.cluster }} has never succeeded"
)

_NEVER_SUCCEEDED_DESCRIPTION = (
    "The CronJob {{ $labels.cronjob }} in namespace {{ $labels.namespace }},"
    " cluster {{ $labels.cluster }}, has existed for more than 10 days, is not"
    " suspended, and has never recorded a successful run. Every staleness rule"
    " is structurally blind to this: kube-state-metrics omits"
    " kube_cronjob_status_last_successful_time until the first success, and an"
    " absent series is NoData rather than a large age. A CronJob that never"
    " fires at all also leaves no Job history to read, so check the schedule and"
    " startingDeadlineSeconds on `kubectl -n {{ $labels.namespace }} describe"
    " cronjob {{ $labels.cronjob }}` before looking for a failing run."
)


def _error_ratio_expr(cluster_filter: str) -> str:
    """Failing share of MCP tool calls over 10 minutes, per cluster.

    Grouped by ``cluster`` alone. The counter carries the pod under
    ``k8s_pod_name`` and the namespace under ``k8s_namespace_name`` rather than
    the ``pod``/``namespace`` names kube-state-metrics uses, and neither is in
    the notification policy's ``group_bies`` -- so aggregating any finer would
    produce labels the router cannot bundle on. There is one witan service per
    stack, which makes ``cluster`` the whole of the resource identity anyway.
    """
    return (
        "(\n"
        "  sum by (cluster) (\n"
        "    rate(witan_tool_calls_total"
        f'{{cluster=~"{cluster_filter}", outcome="error"}}[10m])\n'
        "  )\n"
        "  /\n"
        "  sum by (cluster) (\n"
        f'    rate(witan_tool_calls_total{{cluster=~"{cluster_filter}"}}[10m])\n'
        "  )\n"
        f") > {_ERROR_RATIO_THRESHOLD}"
    )


def _daily_stale_expr(cluster_filter: str) -> str:
    """Age of each daily maintenance CronJob's last success, per cluster.

    The ``> 0`` guard inside the parentheses is the same one eks_general.py's
    staleness rules carry: kube-state-metrics reports 0 until a CronJob's first
    success, and ``time()`` minus zero fires instantly on anything newly
    created. The never-succeeded case is covered separately below rather than
    by removing this guard.
    """
    return (
        "max by (cluster, namespace, cronjob) (time() - "
        "(kube_cronjob_status_last_successful_time"
        f'{{cluster=~"{cluster_filter}", '
        f'cronjob=~"{_DAILY_MAINTENANCE_CRONJOBS}"}} > 0)) '
        f"> {_DAILY_STALE_SECONDS}"
    )


def _never_succeeded_expr(cluster_filter: str) -> str:
    """Unsuspended CronJobs older than the age gate with no recorded success."""
    selector = f'cluster=~"{cluster_filter}", namespace=~"{_WITAN_NAMESPACES}"'
    return (
        "(\n"
        "  (\n"
        "    max by (cluster, namespace, cronjob) (\n"
        f"      kube_cronjob_spec_suspend{{{selector}}}\n"
        "    ) == 0\n"
        "  )\n"
        "  unless on (cluster, namespace, cronjob)\n"
        "    max by (cluster, namespace, cronjob) (\n"
        f"      kube_cronjob_status_last_successful_time{{{selector}}}\n"
        "    )\n"
        ")\n"
        "and on (cluster, namespace, cronjob) (\n"
        "  time() - max by (cluster, namespace, cronjob) (\n"
        f"    kube_cronjob_created{{{selector}}}\n"
        f"  ) > {_NEVER_SUCCEEDED_MIN_AGE_SECONDS}\n"
        ")"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create the witan service alert rule group."""
    alerting.RuleGroup(
        "witan-service",
        name="witan-service",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            # --- Tool-call error rate: the graph-brownout detector ---
            alerting.RuleGroupRuleArgs(
                name="WitanToolCallErrorRateWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _ERROR_RATE_SUMMARY,
                    "description": _ERROR_RATE_DESCRIPTION,
                },
                datas=rd(_error_ratio_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanToolCallErrorRateCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _ERROR_RATE_SUMMARY,
                    "description": _ERROR_RATE_DESCRIPTION,
                },
                datas=rd(_error_ratio_expr(_PROD_CLUSTERS)),
            ),
            # --- Daily maintenance stopped ---
            alerting.RuleGroupRuleArgs(
                name="WitanDailyMaintenanceStaleWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _DAILY_STALE_SUMMARY,
                    "description": _DAILY_STALE_DESCRIPTION,
                },
                datas=rd(_daily_stale_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanDailyMaintenanceStaleCritical",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _DAILY_STALE_SUMMARY,
                    "description": _DAILY_STALE_DESCRIPTION,
                },
                datas=rd(_daily_stale_expr(_PROD_CLUSTERS)),
            ),
            # --- Never succeeded at all ---
            alerting.RuleGroupRuleArgs(
                name="WitanScheduledJobNeverSucceededWarning",
                condition="C",
                for_="1h",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _NEVER_SUCCEEDED_SUMMARY,
                    "description": _NEVER_SUCCEEDED_DESCRIPTION,
                },
                datas=rd(_never_succeeded_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanScheduledJobNeverSucceededCritical",
                condition="C",
                for_="1h",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _NEVER_SUCCEEDED_SUMMARY,
                    "description": _NEVER_SUCCEEDED_DESCRIPTION,
                },
                datas=rd(_never_succeeded_expr(_PROD_CLUSTERS)),
            ),
        ],
        opts=resource_opts,
    )
