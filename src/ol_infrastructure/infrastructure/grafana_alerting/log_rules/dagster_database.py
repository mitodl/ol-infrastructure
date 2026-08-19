"""Dagster client-side database connection failure alert rules.

Written 2026-08-18, after the data-production Dagster daemon spent days unable to
open a socket to PgBouncer -- 28232 of 28232 ephemeral ports consumed by TIME_WAIT
toward the PgBouncer service address, psycopg2 raising "Cannot assign requested
address", and user-visible run failures -- while every rule that existed in
metric_rules/dagster_pgbouncer.py at the time stayed correctly quiet.

Why the concurrency rules could not have caught it
---------------------------------------------------
Those rules measure how much of PgBouncer is occupied: server connections against
max_db_connections, cl_waiting, maxwait, pgbouncer_up. During the incident
PgBouncer read 12 active clients, 11 active server connections, 0 waiting, 40 of
a ~708 per-replica cap -- a pool with nothing wrong with it. The failure was
entirely in the client's own network namespace, upstream of anything PgBouncer
can observe: a client that cannot allocate a source port never becomes a
connection PgBouncer counts.

The two signatures are opposites, not points on one scale. The harder the client
churns, the *emptier* the pool looks, because connections that fail to establish
are connections the pool never sees. No threshold on a concurrency rule can cover
this, which is why this lives in its own module rather than being bolted onto
dagster_pgbouncer.py as a tighter bound on an existing series.

DagsterPgBouncerConnectionChurn was added to that file alongside this module and
is the one pool-side series that does see the churn, earlier than this rule does
-- but it measures arrival rate, a third axis again, and it covers only the one
failure mode. See below. Do not delete any of the three as redundant.

Why a log rule rather than a metric
------------------------------------
The obvious metric -- per-pod ephemeral port / TIME_WAIT consumption -- does not
exist here. node_exporter's node_sockstat_TCP_tw is node-level, and pods have
their own network namespace, so the one series that would name the exhausted
namespace is exactly the one it cannot produce. Nothing in kube-state-metrics or
cAdvisor fills that gap either. Dagster's own retry wrapper, meanwhile, already
logs every failed connection attempt regardless of cause -- DNS, refused, port
exhaustion, pool cap, RDS failover -- so it needs no new plumbing and is not
specific to the one failure mode that prompted it.

Why this outlives the metric rule it is paired with
----------------------------------------------------
DagsterPgBouncerConnectionChurn watches the precondition for port exhaustion and
is the earlier warning, but it only sees that one failure mode. The 2026-08-18
remediation is the proof, and it ran in two phases rather than one:

  until ~16:40Z  non-pooling storages -- churn 415/s, ~270 retries/min,
                 psycopg2 "Cannot assign requested address". Real port exhaustion.
  ~16:50-17:50Z  churn already down at 0.7/s, yet the daemon still could not get
                 connections, at ~30/min and now with a different error entirely:
                 "QueuePool limit of size 10 overflow 10 reached, connection timed
                 out". Client-side SQLAlchemy pool saturation, not the network.
  after ~17:50Z  clean.

The churn metric went quiet 70 minutes before the daemon stopped failing. This
rule spanned both phases because it matches Dagster's retry wrapper rather than
any particular psycopg2 error -- the generality is the point, not laziness about
the filter. Resist narrowing it to the error string of whichever incident is
freshest.

Two windows, same shape as log_rules/apisix_oidc.py
----------------------------------------------------
  fast     100 lines per 10 minutes, confirmed 15m. An acute storm.
  chronic   10 lines per 10 minutes, sustained 2h. A steady bleed.

The fast rule shipped alone and that was a mistake, caught on 2026-08-19 by the
QA re-size it was meant to verify. Its 100/10min came from Production's port
exhaustion, where quiet hours topped out at 54 lines/hour (8, 8, 54, 21, 7 across
the pre-incident samples, ~9 per 10 minutes) and the storm ran 10,000-29,000
lines/hour, ~2,700 per 10 minutes -- a ~500x separation with an enormous gap in
the middle. QA's failure lives in that gap: a continuous 22-147 per 10 minutes,
every evaluation, for hours. When the pool re-size in #5516 cut it from ~120 to
~22-74, the fast rule went INACTIVE while the daemon kept failing every single
minute, and reported health. A rule that goes quiet on an ongoing failure is
worse than no rule, because someone acts on the silence.

So the chronic rule is not a lower threshold for the same thing, it is the other
half of the signal. 10 per 10 minutes is above Production's noisiest quiet
10-minute window (9) and below anything QA has produced while broken (min 22).
Verified against both stacks the day it was written: over three hours QA breached
at every evaluation point while Production returned no rows at all over six.

for_=2h is what makes it a *chronic* rule rather than a twitchy one. Grafana
requires the condition to hold at every evaluation across the whole period, so a
single quiet 10-minute window resets it -- a pod roll, an RDS failover, a
deploy-time blip all clear themselves without paging, while a bleed that never
stops eventually fires. That is also why the fast rule is still worth keeping at
a high threshold: a genuine storm should not wait two hours to page.

Background: docs/plans/dagster-pgbouncer-observability.md.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Lines per 10 minutes, summed across every pod in the namespace. Run workers and
# the user-code servers contribute a handful each (1-20 per 10 minutes at their
# noisiest) on top of the daemon's, which is why neither gate is simply > 0.
#
# See the module docstring for why one threshold could not do both jobs.
_FAST_LINES_PER_10M = 100
_CHRONIC_LINES_PER_10M = 10

# The string Dagster's retry_pg_connection_fn wrapper logs on every failed attempt
# to reach the metadata database, whatever the underlying cause. Matching the
# wrapper rather than a psycopg2 error class is deliberate: "OperationalError"
# alone would also catch "canceling statement due to statement timeout", which is
# a query-side failure against a connection that established perfectly well.
_RETRY_LINE = "Retrying failed database connection"

_DESCRIPTION = (
    "Dagster in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}"
    " is failing to open connections to its metadata database at a sustained rate."
    " This is measured on the client, not on PgBouncer: the pool-side rules in"
    " metric_rules/dagster_pgbouncer.py read normal during exactly this failure,"
    " because a connection that never establishes is one PgBouncer never counts."
    " Check the daemon pod for the psycopg2 error behind the retry. 'Cannot assign"
    " requested address' means the pod has exhausted its ephemeral ports against"
    " the PgBouncer service address -- confirm with `ss -tan state time-wait | wc"
    " -l` in the pod, which will sit at net.ipv4.ip_local_port_range -- and points"
    " at a Dagster storage configured with a non-pooling connection class."
    " 'Connection refused' or a DNS failure points at the path to PgBouncer"
    " rather than at Dagster -- but do not wait for a pool-side alert to confirm"
    " it. The exporter reaches PgBouncer over 127.0.0.1 inside the same pod,"
    " while Dagster reaches it through the Service DNS name and its ClusterIP,"
    " so anything broken between those two points leaves pgbouncer_up at 1 and"
    " DagsterPgBouncerExporterDown silent. Resolve the Service name from a"
    " Dagster pod and connect to it there."
)

_FAST_SUMMARY = (
    "Dagster in {{ $labels.cluster }} logged {{ $value }} failed database"
    " connections in 10 minutes"
)

_CHRONIC_SUMMARY = (
    "Dagster in {{ $labels.cluster }} has been failing database connections"
    " continuously for 2 hours ({{ $value }} in the last 10 minutes)"
)

# Appended to the shared description for the chronic rule only. The fast rule's
# advice is all about finding an acute cause; this one's is about the opposite
# failure -- something small enough to have been running unnoticed.
_CHRONIC_EXTRA = (
    " This rule fires on a low, unbroken rate rather than a spike, so the cause"
    " is more likely a ceiling that is simply too small than anything that"
    " broke: check the QueuePool sizes in dagster_instance.yaml"
    " (dagster:event_log_pool_size and friends) against what the process"
    " actually needs, and read the error on the retry line before assuming it"
    " is the same failure as last time. A 'QueuePool limit of size N overflow M"
    " reached' here means the client's own pool is the binding limit and no"
    " amount of PgBouncer headroom will help."
)

_CHRONIC_DESCRIPTION = _DESCRIPTION + _CHRONIC_EXTRA


_NON_PROD_CLUSTERS = ".*-(ci|qa)"
_PROD_CLUSTERS = ".*-(production)"


def _retry_count_expr(cluster_filter: str, threshold: int) -> str:
    """Build a retry-line count expression for one cluster filter and threshold.

    The window is 10 minutes for both rules. What separates fast from chronic is
    the threshold and the ``for_`` holding it, not the window -- see the module
    docstring.
    """
    return (
        "sum by (cluster, namespace) (\n"
        "  count_over_time(\n"
        f'    {{namespace="dagster", cluster=~"{cluster_filter}"}}\n'
        f'    |= "{_RETRY_LINE}" [10m]\n'
        "  )\n"
        f") > {threshold}"
    )


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Dagster client-side database connection alert rule groups."""
    alerting.RuleGroup(
        "loki-dagster-database-connections",
        name="dagster-database-connections",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            # --- Fast: an acute storm ---
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _FAST_SUMMARY,
                    "description": _DESCRIPTION,
                },
                datas=rd(_retry_count_expr(_NON_PROD_CLUSTERS, _FAST_LINES_PER_10M)),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _FAST_SUMMARY,
                    "description": _DESCRIPTION,
                },
                datas=rd(_retry_count_expr(_PROD_CLUSTERS, _FAST_LINES_PER_10M)),
            ),
            # --- Chronic: a steady bleed the fast rule cannot see ---
            # Deliberately overlapping: during a real storm both fire and the
            # fast one gets there first. The chronic rule exists for the case
            # the fast one structurally misses -- a rate too low to trip 100 but
            # too persistent to be noise -- which is exactly how QA sat broken
            # for hours while the fast rule read healthy on 2026-08-19.
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresChronicWarning",
                condition="C",
                for_="2h",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _CHRONIC_SUMMARY,
                    "description": _CHRONIC_DESCRIPTION,
                },
                datas=rd(_retry_count_expr(_NON_PROD_CLUSTERS, _CHRONIC_LINES_PER_10M)),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresChronicCritical",
                condition="C",
                for_="2h",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _CHRONIC_SUMMARY,
                    "description": _CHRONIC_DESCRIPTION,
                },
                datas=rd(_retry_count_expr(_PROD_CLUSTERS, _CHRONIC_LINES_PER_10M)),
            ),
        ],
        opts=resource_opts,
    )
