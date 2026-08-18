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

Threshold, from 14 days of Loki
--------------------------------
Quiet hours on data-production top out at 54 lines/hour (8, 8, 54, 21, 7 across
the pre-incident samples), or ~9 per 10 minutes. Phase one ran 10,000-29,000
lines/hour, ~2,700 per 10 minutes, continuously from 2026-08-09. 100 per 10
minutes therefore sits an order of magnitude above the noisiest quiet hour and
27x below the incident, on a signal whose two states are separated by ~500x. It
also clears phase two's ~300 per 10 minutes by 3x, so that hour would have paged
too -- correctly, since runs were still failing throughout it.

Background: docs/plans/dagster-pgbouncer-observability.md.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Lines per 10 minutes, summed across every pod in the namespace. Run workers and
# the user-code servers contribute a handful each (1-20 per 10 minutes at their
# noisiest) on top of the daemon's, which is why the gate is not simply > 0.
_RETRY_LINES_PER_10M = 100

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

_SUMMARY = (
    "Dagster in {{ $labels.cluster }} logged {{ $value }} failed database"
    " connections in 10 minutes"
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
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                labels={"severity": "warning", "environment": "non-production"},
                annotations={
                    "summary": _SUMMARY,
                    "description": _DESCRIPTION,
                },
                datas=rd(
                    "sum by (cluster, namespace) (\n"
                    "  count_over_time(\n"
                    '    {namespace="dagster", cluster=~".*-(ci|qa)"}\n'
                    f'    |= "{_RETRY_LINE}" [10m]\n'
                    "  )\n"
                    f") > {_RETRY_LINES_PER_10M}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterDatabaseConnectionFailuresCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="KeepLast",
                labels={"severity": "critical", "environment": "production"},
                annotations={
                    "summary": _SUMMARY,
                    "description": _DESCRIPTION,
                },
                datas=rd(
                    "sum by (cluster, namespace) (\n"
                    "  count_over_time(\n"
                    '    {namespace="dagster", cluster=~".*-(production)"}\n'
                    f'    |= "{_RETRY_LINE}" [10m]\n'
                    "  )\n"
                    f") > {_RETRY_LINES_PER_10M}"
                ),
            ),
        ],
        opts=resource_opts,
    )
