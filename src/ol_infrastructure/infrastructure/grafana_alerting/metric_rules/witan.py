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

WitanDataTierScaledToZero: the state DeploymentUnavailable* excludes
----------------------------------------------------------------------
eks_general.py's ``DeploymentUnavailable*`` requires ``spec_replicas > 0``, so
a Deployment at 0 desired replicas is by its definition not an outage. For
omnigraph-server it is one. Two things scale it to 0: an armed storage-format
migration, and (at storage format 14) the nightly ``omnigraph-maintenance``
window, which leaves it at 0 ON PURPOSE when it cannot remove its own lock.
Either one left that way takes every witan tool call down with a Deployment
that reports nothing wrong.

The pending period is what excludes the window, not a time-of-day match. The
window's Job deadline is 36 minutes and its pod gets 120s more to scale the
server back up, so 45 minutes at 0 replicas is longer than any window that
ended normally, wherever ``omnigraph:maintenance_schedule`` puts it. If that
deadline grows, this has to grow with it.

A migration cutover that keeps the server down for longer than 45 minutes fires
this too. That is correct (the data tier is down) and expected: silence it for
the cutover.

Verification
--------------
★ CHECK THE VALUE, NOT THE ROW SET. base.py's pipeline ends in a threshold on
``last(A) > 0``, so an expression that returns rows carrying 0 never fires. A
filtering comparison in PromQL (``x == 0``, ``x > 5``) returns the matching
series with its ORIGINAL value, and ``and``/``unless`` propagate the LEFT-hand
side's -- so the last thing on the left has to be something positive. That trap
is documented on ``DeploymentUnavailable*`` in eks_general.py and
``DagsterPgBouncerExporterDown`` in dagster_pgbouncer.py, and the first revision
of this module walked into it anyway with ``_never_succeeded_expr``. Querying
the datasource and seeing rows come back is not sufficient evidence; read the
number.

Every expression here was evaluated against the production Mimir tenant on
2026-08-24, and each was shown to be able to FIRE -- to return a row whose
VALUE is greater than zero -- rather than only to be quiet:

  WitanScheduledJobNeverSucceeded  returns nothing as committed. Dropping the
                                   ``unless`` returns the four unsuspended
                                   CronJobs at 1,625,182-1,649,167 (their ages
                                   in seconds); relaxing ``spec_suspend == 0``
                                   to ``>= 0`` returns witan-break-glass at
                                   1,628,376. Under the broken first revision
                                   the same two probes returned 0 and 1 -- the
                                   1 being break-glass's suspend value, not
                                   evidence of anything.
  WitanDailyMaintenanceStale       returned 47,818s for omnigraph-optimize
                                   against its 129,600s threshold when the
                                   threshold was dropped to 1 (last success
                                   13.3h earlier), on both the Production and
                                   QA tenants.
  WitanToolCallErrorRate           the ratio itself is the value, so anything
                                   past the 0.5 threshold is positive by
                                   construction. See the baseline figures
                                   above.

``WitanDataTierScaledToZero`` was evaluated against the production Mimir tenant
on 2026-10-13. As committed it returns a real 0 for omnigraph-server. Over the
30 days before, at 5-minute steps, it returns 1 at 10 of 8,641 steps, all on
2026-09-16: two at 15:46Z-15:51Z and eight from 18:36Z to 19:11Z. That longest
run is under the 45-minute pending period, so the rule would not have fired on
it. The window itself exists nowhere yet, so "longer than any window" rests on
the Job's configured deadline and not on an observed run.
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
#
# `omnigraph-maintenance` replaces `omnigraph-optimize` (and the weekly
# `omnigraph-cleanup`) once an environment serves storage format 14, on the
# same `20 3 * * *` schedule. It does not exist anywhere yet, so its series
# is unvalidated and matches nothing until the first cutover.
_DAILY_MAINTENANCE_CRONJOBS = (
    "omnigraph-optimize|omnigraph-maintenance|witan-view-reaper"
)

# Longer than the longest cadence in these namespaces (weekly), so a newly
# created CronJob is not reported before it could plausibly have succeeded.
_NEVER_SUCCEEDED_MIN_AGE_SECONDS = 864000

_WITAN_NAMESPACES = "witan|omnigraph"

# Longer than the maintenance window can hold the server at 0 replicas: a
# 36-minute Job deadline plus the 120s its pod gets to scale back up
# (applications/omnigraph/maintenance.py).
_SCALED_TO_ZERO_FOR = "45m"

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
    " of them. All of them means the graph; one of them means that tool. A"
    " failed line also says why: error_type, error (the message, replaced by"
    " error_withheld=true where it is not safe to log) and refused."
    " refused=true means witan declined the call on purpose, and this alert"
    " counts those as errors too. A missing `council` graph is NOT a refusal:"
    " it logs error_type=ToolError, refused=false and an `omnigraph ... failed`"
    " message in error. A missing code graph logs ClusterGraphMissing with"
    " refused=true."
)

_DAILY_STALE_SUMMARY = (
    "Daily maintenance job {{ $labels.cronjob }} has not succeeded in"
    " {{ $labels.cluster }} for over 36 hours"
)

_DAILY_STALE_DESCRIPTION = (
    "The daily maintenance CronJob {{ $labels.cronjob }} in namespace"
    " {{ $labels.namespace }}, cluster {{ $labels.cluster }}, has not completed"
    " successfully for over 36 hours. None of these jobs fails loudly when it"
    " stops -- that is why they need a window sized to their cadence rather than"
    " eks_general.py's 15-day slow bucket."
    " omnigraph-maintenance exists only where the data tier serves storage"
    " format 14, in place of omnigraph-optimize. It STOPS omnigraph-server every"
    " night at 03:20 UTC to compact and clean the store, and scales it back up."
    " If this alert names it, first check that the server is running"
    " (`kubectl -n omnigraph get deploy omnigraph-server`): a window that could"
    " not remove its own lock leaves the Deployment at 0 replicas on purpose."
    " The last Job's log says which case it is, and"
    " docs/omnigraph-store-maintenance-runbook.md (When the window leaves the"
    " server down) is the recovery. A Job that failed with the server back up"
    " names the graphs it did not reach; a suspended CronJob means a"
    " storage-format migration is armed."
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

_SCALED_TO_ZERO_SUMMARY = (
    "omnigraph-server in {{ $labels.cluster }} has been scaled to 0 replicas"
    " for over 45 minutes"
)

_SCALED_TO_ZERO_DESCRIPTION = (
    "The omnigraph-server Deployment in cluster {{ $labels.cluster }} has"
    " declared 0 replicas for over 45 minutes, so every witan tool call is"
    " failing. DeploymentUnavailable does not report this: it ignores a"
    " Deployment that asks for 0 replicas."
    " Two things scale it to 0. The nightly omnigraph-maintenance window"
    " (storage format 14 only) does, and leaves it there on purpose when it"
    " cannot remove its own lock: read the last Job's log (`kubectl -n omnigraph"
    " logs job/<newest omnigraph-maintenance job>`) and follow"
    " docs/omnigraph-store-maintenance-runbook.md (When the window leaves the"
    " server down). Do not scale it up before clearing the lock, because the"
    " server refuses to start under it."
    " An armed storage-format migration does too: `kubectl -n omnigraph get"
    " deploy omnigraph-server -o jsonpath='{.metadata.annotations}'` shows"
    " ol.mit.edu/migration-armed at format 14, and"
    " docs/omnigraph-storage-format-upgrade-runbook.md is the procedure. If a"
    " cutover is in progress this is expected, and can be silenced until it"
    " ends."
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


def _scaled_to_zero_expr(cluster_filter: str) -> str:
    """1 while omnigraph-server declares 0 replicas, else a real 0.

    ``== bool 0`` and not ``== 0``: the filtering form returns the series with
    its original value, which is 0, and base.py's ``last(A) > 0`` never fires
    on it. See the module docstring's verification note.
    """
    return (
        "max by (cluster, namespace, deployment) (\n"
        "  kube_deployment_spec_replicas"
        f'{{cluster=~"{cluster_filter}", namespace="omnigraph", '
        'deployment="omnigraph-server"}\n'
        ") == bool 0"
    )


def _never_succeeded_expr(cluster_filter: str) -> str:
    """Unsuspended CronJobs older than the age gate with no recorded success.

    ★ THE AGE TERM IS ON THE LEFT BECAUSE THE VALUE IS WHAT FIRES, NOT THE
    ROW. base.py's pipeline ends in a threshold on ``last(A) > 0``, and a
    filtering comparison in PromQL returns the matching series carrying its
    ORIGINAL value -- so ``kube_cronjob_spec_suspend == 0`` yields a row whose
    value is 0, and 0 > 0 never fires. ``and``/``unless`` both propagate the
    LEFT-hand side's value, so leading with the age (a large positive number of
    seconds) is what makes the rule able to fire at all. This is the same
    ordering, for the same reason, as ``DeploymentUnavailable*`` in
    eks_general.py and ``DagsterPgBouncerExporterDown`` in
    dagster_pgbouncer.py.

    An earlier revision led with the suspend filter and could never have fired.
    It survived a verification pass because that pass checked only whether the
    expression returned ROWS: relaxing ``== 0`` to ``>= 0`` returned
    witan-break-glass, which looked like proof, but break-glass is SUSPENDED
    and so carries a source value of 1 -- the one input in the namespace that
    happens to satisfy the threshold. Caught in review by both
    copilot-pull-request-reviewer and sentry independently. Checking an
    expression against the query API proves the row set; it does not prove the
    value, and only the value reaches stage C.

    The value that now propagates is the CronJob's age in seconds, which is
    also what ``{{ $value }}`` renders in the annotation -- more useful than
    the constant this would otherwise carry.
    """
    selector = f'cluster=~"{cluster_filter}", namespace=~"{_WITAN_NAMESPACES}"'
    return (
        "(\n"
        "  (\n"
        "    time() - max by (cluster, namespace, cronjob) (\n"
        f"      kube_cronjob_created{{{selector}}}\n"
        f"    ) > {_NEVER_SUCCEEDED_MIN_AGE_SECONDS}\n"
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
            # --- Data tier left scaled to zero ---
            # Last in the list so the rules above keep their positions.
            alerting.RuleGroupRuleArgs(
                name="WitanDataTierScaledToZeroWarning",
                condition="C",
                for_=_SCALED_TO_ZERO_FOR,
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _SCALED_TO_ZERO_SUMMARY,
                    "description": _SCALED_TO_ZERO_DESCRIPTION,
                },
                datas=rd(_scaled_to_zero_expr(_NON_PROD_CLUSTERS)),
            ),
            alerting.RuleGroupRuleArgs(
                name="WitanDataTierScaledToZeroCritical",
                condition="C",
                for_=_SCALED_TO_ZERO_FOR,
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _SCALED_TO_ZERO_SUMMARY,
                    "description": _SCALED_TO_ZERO_DESCRIPTION,
                },
                datas=rd(_scaled_to_zero_expr(_PROD_CLUSTERS)),
            ),
        ],
        opts=resource_opts,
    )
