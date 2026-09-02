"""Dagster control-plane alert rules, off the SQL exporter added in #5476.

Companion to dagster_pgbouncer.py. That file measures the pool; this one measures
what the pool exists to serve. The two failure modes the 2026-08-10 incident
produced -- a saturated pool and a daemon 25 minutes behind 178 run workers --
need both, because neither is visible in the other's metrics.

Every threshold here was measured against five days of post-storm series on
data-production, 2026-08-19 19:45Z to 2026-08-24 19:45Z. That window is chosen,
not convenient. Two hazards make an ordinary "last 7 days" window wrong:

  The ol-data-platform retry storm ran until 2026-08-18 03:30Z and its tail
  drains for hours after. Run volume inside it reached 7416 per 6h against a
  quiet-week 96-230, a 50x swing on the same workload, and the failure share sat
  at ~95%. Any threshold fitted across that boundary is fitted to two different
  workloads at once.

  dagster_recent_* meant something different before #5495 (deployed 2026-08-18
  18:03Z). Those queries had no time predicate at all, so dagster_recent_job_ticks
  was a whole-table count rather than "in the last hour". A max_over_time spanning
  that change mixes the two quantities under one name and returns the old id cap
  as if it were a measurement.

QA and CI are absent from the numbers below for a structural reason, not because
their exporters are broken: each Grafana Cloud stack queries its own Mimir tenant
(see base.py), and this file was calibrated against the production stack's. The
warning rules carry the same thresholds as their critical twins because there is
no separate QA baseline to set them from. Revisit if QA's shape turns out to
differ; nothing here assumes it does.

Deliberate omissions
--------------------
dagster_relation_bytes gets no rule. event_logs is the large one at 418 GB and
grew 1.9 GB across the five-day window, ~0.38 GB/day; the four instrumented
relations together add ~0.46 GB/day against max_storage = 1000 GB in
__main__.py, which is roughly three years of runway with storage autoscaling
covering the interim. A trend rule on that would be noise, and free-storage
headroom is a database-level question that belongs to the CloudWatch alarm
profile rather than here.

dagster_runs_in_flight{status="QUEUED"} gets no rule either. Queue depth cannot
separate a healthy burst from a stuck coordinator -- a backfill legitimately
produces a deep queue -- so the depth is measured and dashboarded but the alert
is on dagster_oldest_queued_run_age_seconds instead, which distinguishes them.

Warning rules filter cluster=~".*-(ci|qa)"       -- fire on CI and QA stacks.
Critical rules filter cluster=~".*-(production)"  -- fire on prod stack only.
Rules with no matching data on a given stack stay silent (no_data_state=OK).
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Seconds since a daemon last wrote a heartbeat, above which it counts as stalled.
#
# Dagster writes a heartbeat when its core loop yields, at
# DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30, so this metric is loop-iteration
# latency and not the liveness of some separate thread. A daemon that is alive but
# spending half an hour inside one iteration reads exactly like a dead one here,
# which is the intent: neither is launching work.
#
# The tempting number is Dagster's own DEFAULT_DAEMON_HEARTBEAT_TOLERANCE_SECONDS
# = 1800, the tolerance get_daemon_statuses uses to call a daemon unhealthy in the
# UI (read from dagster/_daemon/controller.py; the same 1800 in every 1.10-1.13
# wheel in the local uv cache, 1.13.17 included).
# Matching it would make the alert and the UI agree. It is still wrong here,
# because the incident this rule exists for was a daemon 25 minutes -- 1500s --
# behind, and 1800 misses that by five minutes.
#
# 600 sits above everything observed and below the failure it has to catch.
# Measured maxima over the five-day window, by daemon_type:
#
#   ASSET                   2492   (see below -- two regimes)
#   SENSOR                   268
#   SCHEDULER                143
#   BACKFILL                 105
#   EVENT_LOG_CONSUMER        98
#   FRESHNESS_DAEMON          96
#   MONITORING                93
#   QUEUED_RUN_COORDINATOR    92
#
# Seven of the eight cluster tightly: p95 28-87s, which is one heartbeat interval,
# exactly what sampling a 30s sawtooth should give. 600 is 2.2x above the worst of
# them.
#
# ASSET is the exception and it is worth reading before responding to this alert.
# Until 2026-08-22 19:20Z its heartbeat age ran a sawtooth peaking at 1600-2500s,
# meaning asset-graph evaluation was taking up to 41 minutes per iteration and
# every automation-condition-driven materialization was delayed by that much. It
# crossed Dagster's own 1800s tolerance repeatedly, which is the threshold
# get_daemon_statuses uses to call a daemon unhealthy, and nothing alerted on any
# of it. What the UI actually displayed at the time was not checked. At 19:20Z it
# dropped to a 47-hour max of 334s and has stayed there. This threshold would have
# fired throughout that period, correctly. If ASSET starts firing again it is the
# same degradation returning, not a miscalibrated rule -- 334 is the number to
# compare against, and the margin over it is only 1.8x.
_DAEMON_HEARTBEAT_SECONDS = 600

# Seconds the oldest queued run has been waiting before the queue counts as stuck
# rather than busy.
#
# QueuedRunCoordinator dequeues on a ~30s cadence and max_concurrent_runs is 80 in
# production against ~520-670 runs/day, so the queue is almost always empty:
# p95 and p99 are both 0 across the window, the mean is 0.55s, and the single
# worst sample in five days is 199.6s. 900 is 4.5x that.
#
# Queue AGE rather than queue DEPTH on purpose. A backfill fills the queue
# legitimately and drains it steadily; a stuck coordinator holds the same run at
# the head of it. Only age tells those apart. A large backfill running against a
# concurrency limit is still the one benign cause of this firing -- check whether
# the oldest run is advancing before treating it as a coordinator fault.
_QUEUED_RUN_AGE_SECONDS = 900

# Share of terminal runs in the exporter's 6h lookback that ended in FAILURE.
#
# Measured across the window: p50 7.5%, p99 17.8%, worst sample 19.6%. During the
# retry storm the same figure was ~95%. 0.40 is 2x the worst observed and less
# than half the storm level, so it distinguishes the two regimes rather than
# splitting the healthy one.
#
# This is the metric that closed the blind spot the exporter was built for.
# kube_job_status_failed saw ~21 failures over the same period the exporter saw
# 18973, and both were right: a Dagster run that fails cleanly still lets its run
# worker exit 0, so the k8s Job counts as SUCCEEDED. Nothing in kube-state-metrics
# can see an application failure.
#
# On the denominator, per les-a-ratio-alert-s-denominator-constrains-what-you-:
# it is the sum of dagster_recent_runs across every status the exporter emits, so
# adding or removing a status from that collector's VALUES list silently rescales
# this rule. The ratio is immune to SQL_EXPORTER_RUN_LOOKBACK changing, which is
# why it is expressed as a ratio and not as a count.
_RUN_FAILURE_RATIO = 0.40

# Minimum terminal runs in the 6h window before the failure ratio is trusted.
#
# Without it the rule is loudest exactly when it knows least: at the quiet-week
# floor of ~150 runs per 6h a ratio is stable, but if volume collapsed to a
# handful, two failures would read as 40% and page. 50 is well under the observed
# floor (p50 SUCCESS alone is 169) so it never suppresses a real signal, and a
# run volume that has fallen below 50 per 6h is its own problem rather than a
# failure-rate one.
_RUN_FAILURE_MIN_VOLUME = 50

# Sensor or schedule ticks in the exporter's 1h lookback that ended in FAILURE.
#
# A sensor that starts erroring stops launching runs and says nothing anywhere
# else; the only evidence is FAILURE rows accumulating in job_ticks. Baseline over
# the window: SENSOR max 1 and mean 0.10 per hour, SCHEDULE and AUTO_MATERIALIZE
# flat 0, against 605-651 total ticks/hour. One genuinely broken sensor evaluating
# on the ~30s daemon cadence produces ~120 failures/hour, so 5 separates the two
# cleanly at 5x the observed maximum.
#
# An absolute count is used rather than a share of ticks because the failing
# population is near zero either way, but it does inherit one sensitivity: the
# tick rate stepped from ~230/hour to a flat 605-651 in a single deploy on
# 2026-08-22 when the sensor set changed. More sensors means more chances to fail,
# so revisit this if the sensor count grows by an order of magnitude.
_TICK_FAILURES = 5

# Multiple of its own lookback below which an id-capped window counts as
# truncating.
#
# dagster_id_window_span_seconds reports the age of the oldest row each id cap
# admits. While the span is comfortably above the lookback, the time predicate is
# what bounds the metric and it covers the period it claims to. If the span falls
# to the lookback, the cap is binding instead and the metric quietly starts
# reporting less than its name says.
#
# It covers exactly two metrics, and neither of them is the one instinct reaches
# for. relation="runs" guards dagster_run_wait_to_start_seconds alone -- the only
# run metric still taking an id cap, because there is no index on start_time to
# drive it off event time. Truncation there shrinks the creation cohort the
# percentiles are computed over, so p50/p95/max stop describing the full lookback.
# relation="job_ticks" guards dagster_recent_job_ticks, where truncation drops
# ticks out of the window and every status count including FAILURE reads low --
# so DagsterJobTickFailures above quietly under-reports, which looks like good
# news.
#
# What it does NOT cover: dagster_recent_runs and dagster_recent_retried_runs
# take no id cap at all, having been moved to a range scan of idx_run_range on
# update_timestamp. DagsterRunFailureRate is therefore unaffected by this rule
# firing, which is worth knowing while responding to it. Assuming otherwise is
# the same mistake an earlier revision of the gauge itself made, one level up.
#
# Currently non-binding by a wide margin: runs spans 7.10 days against a 6h
# lookback (28x) and job_ticks 12.66 hours against 1h (12.7x), the latter
# confirming #5582's SQL_EXPORTER_TICK_WINDOW = 8000 is live. 2x is the point at
# which the caps want raising, not the point at which they have already failed.
#
# The literals below duplicate SQL_EXPORTER_RUN_LOOKBACK and
# SQL_EXPORTER_TICK_LOOKBACK from the dagster stack's __main__.py, which is a
# coupling this file cannot express any other way -- the lookback is baked into
# the collector's SQL and is not exported as a metric. Changing either lookback
# there means changing the matching number here.
_ID_WINDOW_MARGIN = 2
_RUN_LOOKBACK_SECONDS = 6 * 60 * 60
_TICK_LOOKBACK_SECONDS = 60 * 60


def _run_failure_ratio_expr(cluster_filter: str) -> str:
    """Failure share of terminal runs, gated on having enough runs to divide by.

    max by (..., status) collapses the exporter's replicas before anything is
    summed. They all query the same database and report identical values, so
    summing across them would scale both sides of the ratio equally and leave it
    correct, but would scale the volume gate by the replica count -- and that
    count changes during a rollout.
    """
    scope = f'namespace="dagster", cluster=~"{cluster_filter}"'

    def collapsed(selector: str) -> str:
        return (
            "sum by (cluster, namespace) (max by (cluster, namespace, status) "
            f"(dagster_recent_runs{{{selector}}}))"
        )

    failures = collapsed(f'status="FAILURE", {scope}')
    total = collapsed(scope)
    return (
        f"({failures} / {total} > {_RUN_FAILURE_RATIO})"
        f" and {total} > {_RUN_FAILURE_MIN_VOLUME}"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Dagster control-plane alert rule groups."""
    alerting.RuleGroup(
        "dagster-control-plane",
        name="dagster-control-plane",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            # --- Daemon heartbeat staleness ---
            # The 2026-08-10 incident measured directly. Whatever stops a daemon
            # -- an unschedulable pod, a wedged loop, a database it cannot reach
            # -- surfaces here as an age that climbs and does not reset.
            #
            # for_ is 5m rather than the 15m used across dagster_pgbouncer.py.
            # During a genuine stall the age rises monotonically, so a longer for_
            # buys nothing but delay; and 600s is already 20 heartbeat intervals,
            # which nothing reaches transiently.
            alerting.RuleGroupRuleArgs(
                name="DagsterDaemonHeartbeatStaleWarning",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster {{ $labels.daemon_type }} daemon in cluster {{ $labels.cluster }} last heartbeat {{ $value }}s ago",
                    "description": "The {{ $labels.daemon_type }} daemon in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not written a heartbeat for over 600 seconds. Dagster writes one every 30s when its core loop yields, so this is loop-iteration latency: the daemon is either stopped or spending minutes inside a single iteration, and in both cases it has stopped launching work. Check the daemon pod is Running and not Pending -- the 2026-08-10 incident was a daemon 25 minutes behind 178 run workers because its pod could not be scheduled. If daemon_type is ASSET, compare against the 47-hour maximum of 334s: asset-graph evaluation ran at 1600-2500s per iteration until 2026-08-22 and this is what that regression looks like. A daemon type that has been removed from the deployment leaves its row in daemon_heartbeats behind and will age forever; delete the row rather than raising this threshold.",
                },
                datas=rd(
                    "max by (cluster, namespace, daemon_type) "
                    "(dagster_daemon_heartbeat_age_seconds"
                    '{namespace="dagster", cluster=~".*-(ci|qa)"})'
                    f" > {_DAEMON_HEARTBEAT_SECONDS}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterDaemonHeartbeatStaleCritical",
                condition="C",
                for_="5m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster {{ $labels.daemon_type }} daemon in cluster {{ $labels.cluster }} last heartbeat {{ $value }}s ago",
                    "description": "The {{ $labels.daemon_type }} daemon in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has not written a heartbeat for over 600 seconds. Dagster writes one every 30s when its core loop yields, so this is loop-iteration latency: the daemon is either stopped or spending minutes inside a single iteration, and in both cases it has stopped launching work. Check the daemon pod is Running and not Pending -- the 2026-08-10 incident was a daemon 25 minutes behind 178 run workers because its pod could not be scheduled. If daemon_type is ASSET, compare against the 47-hour maximum of 334s: asset-graph evaluation ran at 1600-2500s per iteration until 2026-08-22 and this is what that regression looks like. A daemon type that has been removed from the deployment leaves its row in daemon_heartbeats behind and will age forever; delete the row rather than raising this threshold.",
                },
                datas=rd(
                    "max by (cluster, namespace, daemon_type) "
                    "(dagster_daemon_heartbeat_age_seconds"
                    '{namespace="dagster", cluster=~".*-(production)"})'
                    f" > {_DAEMON_HEARTBEAT_SECONDS}"
                ),
            ),
            # --- Oldest queued run ---
            # The run-coordinator counterpart to the daemon rule: the daemon can be
            # heartbeating perfectly while nothing it dequeues ever starts.
            alerting.RuleGroupRuleArgs(
                name="DagsterQueuedRunStuckWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster's oldest queued run in cluster {{ $labels.cluster }} has been waiting {{ $value }}s",
                    "description": "A run has sat in the queue in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} for over 15 minutes. QueuedRunCoordinator dequeues on a ~30s cadence and the queue is normally empty -- p99 is 0 and the worst sample in five days was 200s. The benign cause is a backfill running against max_concurrent_runs (80 in production): check whether the oldest run is advancing before treating this as a fault. If it is not, the coordinator has stopped dequeuing, and DagsterDaemonHeartbeatStale on daemon_type QUEUED_RUN_COORDINATOR says whether the daemon itself is stalled or whether it is running and unable to launch.",
                },
                datas=rd(
                    "max by (cluster, namespace) "
                    "(dagster_oldest_queued_run_age_seconds"
                    '{namespace="dagster", cluster=~".*-(ci|qa)"})'
                    f" > {_QUEUED_RUN_AGE_SECONDS}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterQueuedRunStuckCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster's oldest queued run in cluster {{ $labels.cluster }} has been waiting {{ $value }}s",
                    "description": "A run has sat in the queue in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} for over 15 minutes. QueuedRunCoordinator dequeues on a ~30s cadence and the queue is normally empty -- p99 is 0 and the worst sample in five days was 200s. The benign cause is a backfill running against max_concurrent_runs (80 in production): check whether the oldest run is advancing before treating this as a fault. If it is not, the coordinator has stopped dequeuing, and DagsterDaemonHeartbeatStale on daemon_type QUEUED_RUN_COORDINATOR says whether the daemon itself is stalled or whether it is running and unable to launch.",
                },
                datas=rd(
                    "max by (cluster, namespace) "
                    "(dagster_oldest_queued_run_age_seconds"
                    '{namespace="dagster", cluster=~".*-(production)"})'
                    f" > {_QUEUED_RUN_AGE_SECONDS}"
                ),
            ),
            # --- Run failure share ---
            # Slow by design. The underlying metric is a 6h sliding window, so it
            # takes hours to move and a for_ shorter than 30m would only add
            # jitter without adding notice.
            alerting.RuleGroupRuleArgs(
                name="DagsterRunFailureRateWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster in cluster {{ $labels.cluster }} is failing {{ $value }} of its terminal runs",
                    "description": "Over 40% of the runs reaching a terminal state in the last 6 hours in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} ended in FAILURE, against a normal 7.5% and a five-day worst case of 19.6%. Do not check kube_job_status_failed to corroborate this: a Dagster run that fails cleanly still lets its run worker exit 0, so the k8s Job reports SUCCEEDED and kube-state-metrics shows nothing. Group the failures by asset and code location in the Dagster UI -- the 2026-08-11 storm that took this to ~95% was a single asset in a single location, re-requested forever by a level-triggered automation condition and multiplied by run_retries.max_retries = 3. Check dagster_recent_retried_runs alongside this: retries rising faster than failures means the same runs are being re-attempted rather than new ones failing.",
                },
                datas=rd(_run_failure_ratio_expr(".*-(ci|qa)")),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterRunFailureRateCritical",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster in cluster {{ $labels.cluster }} is failing {{ $value }} of its terminal runs",
                    "description": "Over 40% of the runs reaching a terminal state in the last 6 hours in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} ended in FAILURE, against a normal 7.5% and a five-day worst case of 19.6%. Do not check kube_job_status_failed to corroborate this: a Dagster run that fails cleanly still lets its run worker exit 0, so the k8s Job reports SUCCEEDED and kube-state-metrics shows nothing. Group the failures by asset and code location in the Dagster UI -- the 2026-08-11 storm that took this to ~95% was a single asset in a single location, re-requested forever by a level-triggered automation condition and multiplied by run_retries.max_retries = 3. Check dagster_recent_retried_runs alongside this: retries rising faster than failures means the same runs are being re-attempted rather than new ones failing.",
                },
                datas=rd(_run_failure_ratio_expr(".*-(production)")),
            ),
            # --- Sensor and schedule tick failures ---
            # The one silent failure in this file. Everything else above shows up
            # somewhere as work not happening; a sensor that errors on every tick
            # simply stops requesting runs, and an empty queue looks like an idle
            # system rather than a broken one.
            alerting.RuleGroupRuleArgs(
                name="DagsterJobTickFailuresWarning",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster {{ $labels.tick_type }} ticks in cluster {{ $labels.cluster }} are failing ({{ $value }} in the last hour)",
                    "description": "More than 5 {{ $labels.tick_type }} ticks failed in the last hour in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}, against a baseline of 0-1 per hour across all tick types. A sensor or schedule that raises on evaluation stops launching runs and reports nothing anywhere else -- the queue simply stays empty, which is indistinguishable from an idle system. A sustained ~120 failures/hour at the ~30s daemon cadence is a broken definition, but a handful is more often the AssetDaemon -- whose ticks land here as tick_type=SENSOR via default_automation_condition_sensor -- erroring against Postgres, so read the daemon log before hunting for a user-authored sensor. Find it in the Dagster UI under the code location's sensors or schedules; the tick will carry the traceback.",
                },
                datas=rd(
                    "max by (cluster, namespace, tick_type) "
                    "(dagster_recent_job_ticks"
                    '{status="FAILURE", namespace="dagster", cluster=~".*-(ci|qa)"})'
                    f" > {_TICK_FAILURES}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterJobTickFailuresCritical",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster {{ $labels.tick_type }} ticks in cluster {{ $labels.cluster }} are failing ({{ $value }} in the last hour)",
                    "description": "More than 5 {{ $labels.tick_type }} ticks failed in the last hour in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}, against a baseline of 0-1 per hour across all tick types. A sensor or schedule that raises on evaluation stops launching runs and reports nothing anywhere else -- the queue simply stays empty, which is indistinguishable from an idle system. A sustained ~120 failures/hour at the ~30s daemon cadence is a broken definition, but a handful is more often the AssetDaemon -- whose ticks land here as tick_type=SENSOR via default_automation_condition_sensor -- erroring against Postgres, so read the daemon log before hunting for a user-authored sensor. Find it in the Dagster UI under the code location's sensors or schedules; the tick will carry the traceback.",
                },
                datas=rd(
                    "max by (cluster, namespace, tick_type) "
                    "(dagster_recent_job_ticks"
                    '{status="FAILURE", namespace="dagster", '
                    'cluster=~".*-(production)"})'
                    f" > {_TICK_FAILURES}"
                ),
            ),
            # --- Exporter self-checks ---
            # Both of the rules below watch the instrumentation rather than the
            # workload, so neither is split by cluster: losing the ability to see
            # production's control plane and losing QA's call for the same
            # response, which is to go and look at the exporter.
            #
            # An id window that has quietly started truncating is the more
            # dangerous of the two, because unlike a dead exporter it leaves every
            # rule above still reporting -- just on less data than the metric
            # names claim, which biases counts downward and makes things look
            # better than they are.
            #
            # The two relations carry different lookbacks, so they need different
            # thresholds; `or` keeps them one rule and $labels.relation says which
            # fired. A span of exactly 0 -- an empty relation -- is not covered,
            # because the shared pipeline in base.py fires on last(A) > 0 and a
            # matching series carrying 0 never clears that. The same trap is
            # documented on DeploymentUnavailable in eks_general.py.
            alerting.RuleGroupRuleArgs(
                name="DagsterSqlExporterIdWindowTruncating",
                condition="C",
                for_="30m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "The Dagster SQL exporter's {{ $labels.relation }} id window has narrowed to {{ $value }}s",
                    "description": "dagster_id_window_span_seconds for {{ $labels.relation }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has fallen to within 2x its own lookback, so the id cap rather than the time predicate is now what bounds the one metric it guards. Check which relation fired: runs guards dagster_run_wait_to_start_seconds alone, where truncation shrinks the creation cohort so its p50/p95/max stop describing the full 6 hours; job_ticks guards dagster_recent_job_ticks, where truncation drops ticks out of the window so every status count including FAILURE reads low and DagsterJobTickFailures quietly under-reports. Neither covers dagster_recent_runs or dagster_recent_retried_runs -- those take no id cap at all, so DagsterRunFailureRate is unaffected either way. Raise SQL_EXPORTER_RUN_WINDOW or SQL_EXPORTER_TICK_WINDOW in the dagster stack's __main__.py to restore the margin. For reference the spans were 7.10 days for runs against a 6h lookback and 12.66 hours for job_ticks against 1h when these thresholds were set. Note the lookback values are duplicated in dagster_control_plane.py because they live in the collector's SQL and are not exported; changing one means changing both.",
                },
                datas=rd(
                    "min by (cluster, namespace, relation) "
                    '(dagster_id_window_span_seconds{relation="runs", '
                    'namespace="dagster"})'
                    f" < {_ID_WINDOW_MARGIN * _RUN_LOOKBACK_SECONDS}"
                    " or "
                    "min by (cluster, namespace, relation) "
                    '(dagster_id_window_span_seconds{relation="job_ticks", '
                    'namespace="dagster"})'
                    f" < {_ID_WINDOW_MARGIN * _TICK_LOOKBACK_SECONDS}"
                ),
            ),
            # Every rule in this file uses no_data_state=OK, which is right for a
            # stack whose Mimir tenant has no Dagster clusters in it but also
            # means a dead exporter takes all of them silently with it. Written as
            # (1 - up) rather than "up == 0" for the reason given above: a series
            # carrying its own 0 never clears last(A) > 0.
            alerting.RuleGroupRuleArgs(
                name="DagsterSqlExporterDown",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "The Dagster SQL exporter on {{ $labels.pod }} is not being scraped successfully",
                    "description": "up is 0 for the dagster-sql-exporter target on pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}, so no Dagster control-plane metrics are being produced and every rule in this group is blind rather than quiet. The exporter assembles its DSN at container start from a Vault dynamic credential, so a failure here is usually the credential or the database rather than the process: check the pod logs for a connection error before restarting anything. A target that disappears entirely takes this rule with it, which only a pipeline-level check would catch.",
                },
                datas=rd(
                    "max by (cluster, namespace, pod) "
                    '(1 - up{job="dagster-sql-exporter"})'
                    " > 0"
                ),
            ),
        ],
        opts=resource_opts,
    )
