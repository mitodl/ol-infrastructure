"""Dagster PgBouncer pool alert rules.

New in 2026-08, off the back of the 2026-08-10 connection-exhaustion incident on
ol-etl-db-production: DatabaseConnections sat at exactly 4989 -- arithmetically the
ceiling on a db.r7g.2xlarge (5000 - 3 superuser_reserved - 2 reserved, the remaining 6
being RDS's own rdsadmin sessions) -- for 88 consecutive 1-minute samples, during which
every new Dagster connection was refused and the daemon sat Pending behind 178 run
workers. Nothing alerted, because nothing was collected: the only signal available was
CloudWatch DatabaseConnections, which min_pool_size x replicas pins to a constant floor
(900 in production) and which therefore reports a configured constant rather than
observed demand. Background: docs/plans/dagster-pgbouncer-observability.md.

Two things had to land before these rules could exist, and both did in #5426:
the pgbouncer_exporter sidecar that produces these metrics, and the max_db_connections
cap that gives the headroom rule a denominator worth measuring against.

Warning rules filter cluster=~".*-(ci|qa)"      -- fire on CI and QA stacks.
Critical rules filter cluster=~".*-(production)" -- fire on prod stack only.
Rules with no matching data on a given stack stay silent (no_data_state=OK).

Why the denominator is a metric rather than a literal
-----------------------------------------------------
pgbouncer_databases_max_connections is PgBouncer reporting its own max_db_connections
back from SHOW DATABASES, so the ratio stays correct without this file knowing anything
about instance classes. That matters because the cap is derived per environment from
postgres_max_connections(instance_size) / pgbouncer_replica_count in the dagster stack:
production's db.r7g.2xlarge yields 708 per pod across 6 replicas (4248 aggregate),
QA's db.m7g.large yields 382 across 2 (764). A literal 5000 in this file would be
silently wrong on every stack but production, which is exactly the class of bug the
cap itself was written to avoid.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# Fraction of the aggregate max_db_connections cap at which the pool is considered
# short of headroom. Production's floor is min_pool_size (150) x 6 replicas = 900
# against a 4248 cap, so the baseline ratio is 0.21 and 3 days of 1-minute samples
# never moved off it -- peak server_active over that window was 122. 0.75 therefore
# sits ~3.5x above anything observed while still leaving a quarter of the pool in
# reserve, which at 6 replicas is over 1000 connections of runway to act in.
_HEADROOM_RATIO = 0.75

# Seconds the oldest queued client has been waiting before the queue counts as
# genuinely stuck rather than briefly backed up. Queries through this pool run in
# under a millisecond (measured: xact 988us, query 918us), so any wait measured in
# whole seconds is already pathological; 5 with for_=10m means a queue that has
# failed to drain for ten minutes, not a transient blip during a server_lifetime
# recycle. Baseline in production is a flat 0.
_MAXWAIT_SECONDS = 5


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Dagster PgBouncer pool alert rule groups."""
    alerting.RuleGroup(
        "dagster-pgbouncer",
        name="dagster-pgbouncer",
        folder_uid=folder_uid,
        interval_seconds=60,
        rules=[
            # --- Aggregate connection headroom ---
            # The alert that would have fired on 2026-08-10. Sums the server
            # connections PgBouncer actually holds against RDS across every replica
            # and compares them to the aggregate max_db_connections cap.
            #
            # Both sides come from the same SHOW DATABASES row, so a pod that is
            # missing from one is missing from the other and the ratio stays honest
            # while replicas roll. database="dagster" excludes the admin console's
            # own "pgbouncer" pseudo-database, which reports the same cap value and
            # would otherwise double the denominator.
            alerting.RuleGroupRuleArgs(
                name="DagsterPgBouncerConnectionHeadroomWarning",
                condition="C",
                for_="15m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster PgBouncer in cluster {{ $labels.cluster }} is holding {{ $value }} of its aggregate connection cap",
                    "description": "PgBouncer's server connections across all replicas in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} have exceeded 75% of the aggregate max_db_connections cap. Past the cap, PgBouncer queues clients rather than opening more backends, and query_wait_timeout is 600, so a client that cannot be served within 10 minutes is disconnected and Dagster sees a connection error -- check DagsterPgBouncerClientsWaiting. Either demand has genuinely grown (raise pgbouncer_replica_count or the RDS instance class, which raises the derived cap with it) or something is holding sessions open: pool_mode is session, so a client that stays connected pins its backend for as long as it lives.",
                },
                datas=rd(
                    "sum by (cluster, namespace) "
                    '(pgbouncer_databases_current_connections{database="dagster", cluster=~".*-(ci|qa)"})'
                    " / "
                    "sum by (cluster, namespace) "
                    '(pgbouncer_databases_max_connections{database="dagster", cluster=~".*-(ci|qa)"})'
                    f" > {_HEADROOM_RATIO}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterPgBouncerConnectionHeadroomCritical",
                condition="C",
                for_="15m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster PgBouncer in cluster {{ $labels.cluster }} is holding {{ $value }} of its aggregate connection cap",
                    "description": "PgBouncer's server connections across all replicas in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} have exceeded 75% of the aggregate max_db_connections cap. Past the cap, PgBouncer queues clients rather than opening more backends, and query_wait_timeout is 600, so a client that cannot be served within 10 minutes is disconnected and Dagster sees a connection error -- check DagsterPgBouncerClientsWaiting. Either demand has genuinely grown (raise pgbouncer_replica_count or the RDS instance class, which raises the derived cap with it) or something is holding sessions open: pool_mode is session, so a client that stays connected pins its backend for as long as it lives.",
                },
                datas=rd(
                    "sum by (cluster, namespace) "
                    '(pgbouncer_databases_current_connections{database="dagster", cluster=~".*-(production)"})'
                    " / "
                    "sum by (cluster, namespace) "
                    '(pgbouncer_databases_max_connections{database="dagster", cluster=~".*-(production)"})'
                    f" > {_HEADROOM_RATIO}"
                ),
            ),
            # --- Clients queued behind the cap ---
            # The headroom rule above says the pool is close to full; this one says
            # the fullness is costing something. It is a separate rule rather than a
            # tighter threshold on the same one because the cap converts database
            # exhaustion into in-pool queuing by design, and query_wait_timeout
            # (600s, chosen so clients ride out RDS checkpoint I/O instead of
            # getting "server closed the connection unexpectedly") means a
            # saturated pool spends ten minutes looking healthy before it starts
            # failing queries. Without this rule that window is invisible: the
            # client side shows nothing wrong until work begins to drop.
            #
            # maxwait is instantaneous -- the age of the oldest client currently in
            # the queue, reset when the queue drains -- so a value that stays high is
            # a queue that is not draining, and one that tracks wall-clock 1:1 is a
            # single client that has never been served.
            alerting.RuleGroupRuleArgs(
                name="DagsterPgBouncerClientsWaitingWarning",
                condition="C",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "Dagster PgBouncer clients in cluster {{ $labels.cluster }} have been queued for {{ $value }}s waiting for a backend",
                    "description": "The oldest client waiting for a server connection in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been queued for over 5 seconds for 10 minutes. Queries through this pool normally complete in under a millisecond. query_wait_timeout is 600, so a client still unserved at 10 minutes is disconnected and the query fails. Sustained waiting at this level means the pool is saturated and Dagster work is being dropped, not merely slowed. Check SHOW POOLS on the pods: sv_idle at 0 with sv_active at the cap means every backend is pinned by a live session.",
                },
                datas=rd(
                    "max by (cluster, namespace) "
                    '(pgbouncer_pools_client_maxwait_seconds{namespace="dagster", cluster=~".*-(ci|qa)"})'
                    f" > {_MAXWAIT_SECONDS}"
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DagsterPgBouncerClientsWaitingCritical",
                condition="C",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "summary": "Dagster PgBouncer clients in cluster {{ $labels.cluster }} have been queued for {{ $value }}s waiting for a backend",
                    "description": "The oldest client waiting for a server connection in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }} has been queued for over 5 seconds for 10 minutes. Queries through this pool normally complete in under a millisecond. query_wait_timeout is 600, so a client still unserved at 10 minutes is disconnected and the query fails. Sustained waiting at this level means the pool is saturated and Dagster work is being dropped, not merely slowed. Check SHOW POOLS on the pods: sv_idle at 0 with sv_active at the cap means every backend is pinned by a live session.",
                },
                datas=rd(
                    "max by (cluster, namespace) "
                    '(pgbouncer_pools_client_maxwait_seconds{namespace="dagster", cluster=~".*-(production)"})'
                    f" > {_MAXWAIT_SECONDS}"
                ),
            ),
            # --- Exporter health ---
            # Both rules above use no_data_state=OK, which is right for a stack whose
            # Mimir tenant simply has no Dagster clusters in it, but it also means an
            # exporter that stops answering takes the connection alerting silently
            # with it. pgbouncer_up is the exporter's own verdict on whether it could
            # reach the admin console, so this covers the case that matters most --
            # PgBouncer alive but unreadable -- while the scrape itself disappearing
            # remains a gap that only a pipeline-level check would close.
            #
            # Written as (1 - pgbouncer_up) rather than the more natural
            # "pgbouncer_up == 0" because the shared pipeline in base.py ends in a
            # threshold that fires on last(A) > 0: "== 0" returns the matching
            # series carrying its own value of 0, and 0 > 0 never fires. Same trap
            # documented at length on DeploymentUnavailable in eks_general.py.
            #
            # No cluster split: this is a monitoring-health signal rather than a
            # workload one, and losing sight of production's pool is not more or
            # less page-worthy than losing sight of QA's -- in both cases the
            # response is to go look at the exporter, not at Dagster.
            alerting.RuleGroupRuleArgs(
                name="DagsterPgBouncerExporterDown",
                condition="C",
                for_="15m",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "summary": "The pgbouncer_exporter sidecar on {{ $labels.pod }} cannot reach PgBouncer's admin console",
                    "description": "pgbouncer_up is 0 on pod {{ $labels.pod }} in namespace {{ $labels.namespace }} in cluster {{ $labels.cluster }}, so no pool metrics are being produced for that replica and the connection-headroom alerts are partially blind. Check that ignore_startup_parameters still includes extra_float_digits -- the exporter's PostgreSQL driver sends it on connect and PgBouncer rejects unrecognised startup parameters, which is the failure mode that shows up as a healthy PgBouncer with a dead exporter.",
                },
                datas=rd(
                    "max by (cluster, namespace, pod) "
                    '(1 - pgbouncer_up{namespace="dagster"})'
                    " > 0"
                ),
            ),
        ],
        opts=resource_opts,
    )
