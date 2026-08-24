# Dagster + PgBouncer observability: what we can collect and what it would let us tune

Investigation of `src/ol_infrastructure/applications/dagster/__main__.py` (PgBouncer deployment)
and the Dagster control plane, against the live `data-production` cluster and
`ol-etl-db-production` RDS instance, on 2026-08-14.

## TL;DR

We currently collect **zero** application-level metrics from either PgBouncer or Dagster.
Every pool-sizing number in `__main__.py` — `default_pool_size = 800`,
`min_pool_size = 150`, `reserve_pool_size = 2000`, `max_client_conn = 1500`,
`pgbouncer_replica_count: 6` — was set without a feedback signal and has never been
validated against observed demand.

The one metric we *do* have (CloudWatch `DatabaseConnections`) is structurally incapable
of answering the tuning question, because `min_pool_size` pins it to a constant floor.

**The instrumentation is still the right project, but it is no longer the most urgent
thing in it.** Reading the 1-minute series and the live pool state turned up a defect that
needs no telemetry to act on: `max_db_connections` and `max_user_connections` are both 0,
so PgBouncer has no aggregate ceiling and its configuration permits up to 9000 backend
connections against an RDS hard limit of 5000. On 2026-08-10 it reached that limit and
held there for 88 consecutive minutes. Setting `max_db_connections` is a one-line fix and
should lead this work, not trail it. Details in
[the configuration section](#the-configuration-permits-this--there-is-no-aggregate-ceiling-anywhere).

## Current state

### What is collected today

Confirmed by querying the Grafana Cloud Prometheus datasource (`grafanacloud-prom`) for
everything carrying `namespace="dagster", cluster="data-production"`:

- **kube-state-metrics** — `kube_job_*`, `kube_pod_*`, `kube_deployment_*` (~2000 Job series)
- **cAdvisor** — `container_cpu_*`, `container_memory_*`, `container_network_*`
- **OpenCost / Kepler** — cost and power allocation

That is the complete list. `count by (__name__) ({namespace="dagster"})` returns nothing
but infrastructure metrics. A regex search for any metric name matching
`.*(pgbouncer|dagster).*` across the whole datasource returns `[]`.

The cluster's collection pipeline is the Grafana `k8s-monitoring` v4 chart
(`src/ol_infrastructure/substructure/aws/eks/grafana.py`), with both
`annotationAutodiscovery` (scrapes `prometheus.io/scrape` annotations) and
`prometheusOperatorObjects` (scrapes `ServiceMonitor`/`PodMonitor` CRs) **already enabled**
and remote-writing to Grafana Cloud. So there is no pipeline work to do — anything that
exposes a `/metrics` endpoint gets collected automatically.

### Why the RDS connection metric can't answer the question

`ol-etl-db-production` has `PerformanceInsightsEnabled = False` and
`MonitoringInterval = 0` — explicitly disabled in `__main__.py:403-406`. So the database
side is limited to basic CloudWatch.

14 days of `DatabaseConnections` (daily min / max):

| Date | Min | Max |
|---|---|---|
| 07-31 | 885 | 901 |
| 08-01 | 900 | 901 |
| 08-02 | 842 | 902 |
| 08-03 | 857 | 965 |
| 08-04 | 628 | 906 |
| 08-05 | 766 | 965 |
| 08-06 | 760 | 902 |
| 08-07 | 778 | 1350 |
| 08-08 | 899 | 1269 |
| 08-09 | 900 | 2230 |
| 08-10 | 801 | **4989** |
| 08-11 | 751 | 901 |
| 08-12 | 806 | 1249 |
| 08-13 | 750 | 1243 |

Three things fall out of this.

**The baseline of 900 is an artifact, not a measurement.** `min_pool_size = 150` × 6
replicas = 900. PgBouncer holds those server connections open permanently regardless of
demand — `server_idle_timeout = 120` will not reduce a pool below `min_pool_size`. The
number is flat at 900 for days at a time because it is a configured constant being
reported back to us. It tells us nothing about how many backends the workload actually
needs, which is exactly the number we would tune `default_pool_size` and
`pgbouncer_replica_count` against.

**On 2026-08-10 we did not come close to connection exhaustion — we hit it, and sat
there for 90 minutes.** The parameter group sets
`max_connections = LEAST({DBInstanceClassMemory/9531392}, 5000)`; on a `db.r7g.2xlarge`
(64 GiB) that evaluates to the 5000 cap. Pulling the 1-minute series (rather than the
daily rollup) makes the shape unambiguous:

```
19:18 EDT  1955      steady state (note: ~1900-2000, not 900)
19:27       996      collapse
19:30      3581      recovery overshoot — +2585 in three minutes
19:48      4828
20:06      4989  ┐
   …       4989  │  flat, every single 1-min sample, for 88 minutes
21:33      4989  ┘
21:36      3245      release
21:36–23:57  2400-4900  oscillating, hours of aftershock
```

4989 is not "near" the limit; it *is* the limit. Postgres reserves
`superuser_reserved_connections = 3` and `reserved_connections = 2`, leaving 4995 for
normal roles, and the remaining 6 are RDS's own `rdsadmin` sessions. A metric that is
bit-identical across 88 consecutive samples is a value being clamped, not measured — so
for that hour and a half the database was continuously saturated. New connections were
not uniformly refused: as short-lived sessions closed, some clients won the released
slots and others got `FATAL: sorry, too many clients already`. Connecting had become a
race rather than a given, which is enough to explain the incident documented in
`dagster_instance.yaml` (daemon Pending for 25 minutes behind 178 run workers).

There is currently no metric, no dashboard, and no alert on this.

### The configuration permits this — there is no aggregate ceiling anywhere

Confirmed against the running pods via `SHOW CONFIG` on the admin console:

| Setting | Value | Per-pod bound | × 6 replicas |
|---|---|---|---|
| `max_client_conn` | 1500 | 1500 clients | **9000** |
| `default_pool_size` | 800 | 800 servers | 4800 |
| `reserve_pool_size` | 2000 | +2000 servers | (2800 → **16 800**) |
| `max_db_connections` | **0 (unlimited)** | — | — |
| `max_user_connections` | **0 (unlimited)** | — | — |
| RDS `max_connections` | | | **5000 (hard)** |

`max_db_connections` and `max_user_connections` are both left at 0, so nothing in the
PgBouncer layer bounds the total. In `pool_mode = session` a client holds a server
connection for its whole session, so the binding constraint is
`min(max_client_conn, default_pool_size + reserve_pool_size)` = 1500 per pod = **9000
aggregate, 1.8× the database's hard limit**. The pool is configured such that it can
exhaust the database it exists to protect, and on 08-10 it did.

This also bears on one of the questions listed further down as needing the exporter to
answer: *is `reserve_pool_size = 2000` ever entered?* Almost certainly yes. The plateau
sits above `default_pool_size × 6 = 4800`, and while that count includes non-PgBouncer
sessions (`rdsadmin`, ad-hoc `psql`), the excess is far larger than those account for.
Note what it does and does not establish: an aggregate above 4800 means *at least one*
replica went past its `default_pool_size` into reserve — it cannot show that all six did,
because the per-pod breakdown was never recorded. That distribution is exactly what
`pgbouncer_pools_server_used_connections` per pod would have told us, and is a good
example of why the aggregate CloudWatch metric cannot answer pool-tuning questions.

**Setting `max_db_connections` is a config-level fix that requires no new telemetry.**
At 6 replicas, a per-pod cap of ~700 puts the aggregate ceiling at 4200 — comfortably
inside 5000 with room for `rdsadmin`, ad-hoc `psql`, and any future consumer — and
converts "exhaust the database and take down the daemon" into "queue inside PgBouncer",
which is the failure mode `query_wait_timeout = 0` was already chosen to tolerate. This
should ship alongside the exporter, not after it.

### Steady state, measured live on all 6 replicas (2026-08-14)

`SHOW POOLS` across every pod, summed:

| | Total | Configured limit | Utilisation |
|---|---|---|---|
| `cl_active` (clients) | 35 | 9000 | 0.4% |
| `sv_active` (servers busy) | 27 | 4800 | 0.6% |
| `sv_idle + sv_used` (held) | 900 | — | = `min_pool_size` × 6 |
| `maxwait` | 0 on every pod | | |

900 server connections are held open to service 27 active ones — a 33:1 idle ratio, and
exactly the `min_pool_size = 150 × 6` artifact predicted above. Between this and the
08-10 plateau, the workload's real range is roughly 27 → 4989 concurrent, which is why
a static floor is the wrong instrument in both directions: too high for the trough, and
irrelevant at the peak.

### What PgBouncer is actually doing right now

PgBouncer already writes an aggregated stats line every 60s (`log_stats` defaults to 1,
`stats_period` to 60). From `dagster-pgbouncer-687c7dd97f-522sx`:

```
LOG stats: 126 xacts/s, 128 queries/s, 0 client parses/s, 0 server parses/s, 0 binds/s,
           in 223328 B/s, out 1965825 B/s, xact 988 us, query 918 us, wait 0 us
```

~126 queries/s per replica (≈750/s across 6), sub-millisecond query time, and
**`wait 0 us`** — no client is queuing for a server connection. Combined with
~30 mCPU and ~15 MiB working set per pod (against requests of 100m / 128Mi), the pool
is not under any throughput pressure at steady state.

### The log-volume problem

`log_connections = 1` and `log_disconnections = 1` (`__main__.py:662-663`) are producing
**27,738 lines in 3 minutes from a single pod** — ~154 lines/s per replica, ~925/s across
six, on the order of 10⁸ lines/day shipped to Grafana Cloud Loki. Sampled content:

```
LOG C-0x...: dagster/v-k8s-data-app-...@10.3.153.131:51454 login attempt: db=dagster ...
LOG C-0x...: dagster/v-k8s-data-app-...@10.3.153.131:51454 closing because: client close request (age=0s)
```

Every connection has `age=0s`. This is Dagster's `NullPool` behaviour — a fresh
connection per query — and it is why `max_client_conn` had to be set so high. The useful
signal (one `stats:` line per minute per pod) is 1 line in ~9,000.

## What to collect

### 1. PgBouncer — `pgbouncer_exporter` sidecar

[`prometheus-community/pgbouncer_exporter`](https://github.com/prometheus-community/pgbouncer_exporter)
(v0.12.1, 2026-06-26) polls the admin console — `SHOW LISTS / STATS / POOLS / DATABASES` —
and serves Prometheus metrics on `:9127/metrics`.

**No PgBouncer config change is needed for auth — verified live, not just from the docs.**
The [docs](https://www.pgbouncer.org/config.html) state that under `auth_type = any`
"the console database allows any user to log in as admin", and running exactly the
connection string the exporter would use against a production pod confirms it:

```
$ kubectl -n dagster exec dagster-pgbouncer-… -c pgbouncer -- \
    psql "postgres://exporter@127.0.0.1:5432/pgbouncer?sslmode=disable" -c "SHOW POOLS;"
 database  | user  | cl_active | … | sv_idle | sv_used | maxwait | …
 dagster   | v-k8s-data-app-… |  5 | … |  24 |  124 |  0 | …
```

`admin_users` / `stats_users` being empty is not a blocker.

**One ConfigMap change is required, though — for a different reason.** The exporter's
PostgreSQL driver sends `extra_float_digits` as a startup parameter, and PgBouncer rejects
startup parameters it does not recognise. `SHOW CONFIG` on the live pods showed
`ignore_startup_parameters` empty, so the template needs
`ignore_startup_parameters = extra_float_digits` or the exporter cannot connect at all.
The exporter's README calls this out; it is easy to miss when reasoning only about auth.

Shape of the change in `__main__.py`:

- Add `PGBOUNCER_EXPORTER_VERSION` to `src/bridge/lib/versions.py`
- Second container in `pgbouncer_deployment` (`__main__.py:747`), port 9127
- Add a `metrics` port to `pgbouncer_service` (`__main__.py:808`)
- A `ServiceMonitor` CustomResource following the pattern already established in
  `src/ol_infrastructure/applications/clickhouse/__main__.py:990` — including the
  `"release": "prometheus"` label

The metrics that answer each open tuning question:

| Question | Metric |
|---|---|
| Is `default_pool_size = 800` anywhere near right? | `pgbouncer_pools_server_active_connections` peak vs. the limit |
| Is `min_pool_size = 150` × 6 replicas justified? | `pgbouncer_pools_server_idle_connections` — the permanently-parked 900 |
| Is `reserve_pool_size = 2000` ever entered? | `pgbouncer_pools_server_used_connections` above `default_pool_size` |
| Is `max_client_conn = 1500` a real ceiling? | `pgbouncer_pools_client_active_connections` |
| Was disabling `query_wait_timeout` the right call? | `pgbouncer_pools_client_waiting_connections`, `pgbouncer_pools_client_maxwait_seconds` |
| Do we need 6 replicas? | per-pod `pgbouncer_stats_queries_total` rate vs. CPU |
| Are we heading for another 08-10? | `sum(pgbouncer_pools_server_*)` against `max_connections` |

`maxwait` in particular is the number PgBouncer tuning turns on, and it is the one thing
we cannot get from the stats log line at any useful resolution. It is also the signal for
whether the new `max_db_connections` cap is set too tight.

Pin the exporter at **>= v0.12.1**: earlier releases report the `reserve_pool` metric
incorrectly against PgBouncer >= 1.24, and `PGBOUNCER_VERSION` is 1.25.2.

#### The answers (2026-08-17, seven days of 1-minute samples on production)

The exporter shipped in #5426; this is what it said. Every number is a max over the
full window unless stated.

| Question | Answer |
|---|---|
| Is `default_pool_size = 800` anywhere near right? | No — it was never reachable. The derived `max_db_connections` is 708/pod, so 800 could not bind on production. Peak `sv_active` was **122 aggregate**. |
| Is `min_pool_size = 150` × 6 justified? | **No.** Held connections sat at a flat **900 every single sample, all week**, and the pool never once grew past its own floor. 900 parked to serve a peak of 122. |
| Is `reserve_pool_size = 2000` ever entered? | Not since the cap landed, and it cannot be: 800 + 2000 per pod is far above the 708 cap, so it was dead configuration. |
| Is `max_client_conn = 1500` a real ceiling? | No — peak `cl_active` was **129 aggregate, 35 on the busiest pod**. It is deliberately far above demand so a saturated pool queues rather than refuses. |
| Was disabling `query_wait_timeout` the right call? | No, and it was already reverted to 600 in #5454 after QA deadlocked for days. `maxwait` was **0 at every sample** on production; `client_waiting` peaked at 1. |
| Do we need 6 replicas? | Not for load — peak **0.09 CPU cores** and 35 active clients on the busiest pod. See below. |
| Are we heading for another 08-10? | Not on this evidence: 122 against a 4248 aggregate cap, and the cap now makes the 08-10 mode structurally unreachable. |

**Skew: production does not have QA's.** The QA deadlock measured 221 active servers on
one replica against 4 on the other, which matters because `max_db_connections` is derived
as budget ÷ replica count and therefore assumes even distribution — under skew the real
ceiling is whichever pod saturates first, not the aggregate. Production was checked
specifically for this before re-tuning and spreads evenly (1–18 active per pod at any
instant). So the aggregate cap is an honest number here, and fewer/larger replicas is an
option rather than a fix that is owed.

**What changed as a result.** `min_pool_size` 150 → 40 (sized to the measured 35-per-pod
peak, so nothing in normal operation waits on a connect; parked total 900 → 240).
`default_pool_size` → the derived cap and `reserve_pool_size` → 0, which retires two dead
numbers and leaves `max_db_connections` as the pool's single binding ceiling. That last
part is not cosmetic: `DagsterPgBouncerConnectionHeadroom` alerts on a fraction of
`pgbouncer_databases_max_connections`, so any pool number set *below* the cap would
become the real limit while the alert kept measuring against one the pool could no longer
reach — a rule that can never fire.

`pgbouncer_replica_count` was left at 6 on purpose. Changing the multiplier in the same
pass as `min_pool_size` would make the result unattributable, which is the habit this
whole exercise exists to break. It is the obvious next lever once this change has a week
of its own data: at 6 replicas a single hot pod saturates at 708 while 3540 connections
sit idle on the other five.

### 2. RDS — re-enable Performance Insights

The exporter gives us the pool's view; it does not tell us what those connections are
*doing* on the database. `rds_defaults["performance_insights_enabled"] = False` at
`__main__.py:405` means we cannot attribute the 08-10 event to a query, a lock, or a
checkpoint. PI's free tier (7-day retention) is enough, and it exposes `DBLoad` by wait
event — the counterpart signal to `maxwait`.

**Pricing and rollout risk both resolved; there is no reason to defer this.**

- *Cost.* [AWS docs](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.Overview.cost.html):
  "Performance Insights includes 7 days of performance data history and 1 million API
  requests per month" by default. The earlier worry that connection cardinality might
  push the dimensional data past a free allowance was unfounded — the free tier bills on
  *retention* and *API requests*, not dimensions, and we would consume neither beyond the
  allowance. Keep `Database Insights` in **Standard** mode; Advanced mode forces 15-month
  retention and is the thing that actually costs money.
- *Downtime.* Toggling Performance Insights on an RDS DB instance
  [does not require a reboot](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.Enabling.html).
  Safe to apply to `ol-etl-db-production` in place.

### 3. Dagster — no native metrics exist; the DB is the source of truth

Dagster OSS has no `/metrics` endpoint on the webserver, daemon, or code locations, and
no OpenTelemetry instrumentation. This is confirmed as an open, uncommitted feature
request ([#4480](https://github.com/dagster-io/dagster/issues/4480),
[#28485](https://github.com/dagster-io/dagster/discussions/28485),
[#11191](https://github.com/dagster-io/dagster/issues/11191)); the `dagster-prometheus`
package is a push-mode resource for use *inside* ops, not server telemetry.

The practical source is the metadata database we already run. Verified against
`dagster==1.13.17`:

- `runs` → `run_id, pipeline_name, status, start_time, end_time, create_timestamp, partition, backfill_id`
- `run_tags` → `run_id, key, value` (carries the code location)
- `job_ticks` → `status, type` (SENSOR / SCHEDULE), `timestamp`
- `jobs` → `job_type, status`

A `sql_exporter` / `postgres_exporter` deployment with custom queries against these gives
the metrics that actually drive Dagster tuning:

| Question | Query |
|---|---|
| Is `max_concurrent_runs = 100` the binding constraint? | `count(*) from runs where status = 'QUEUED'` |
| How long do runs wait before starting? | `start_time - create_timestamp` percentiles |
| Which code locations fail, and how often? | runs by `status` joined to `run_tags` |
| Are sensors/schedules erroring silently? | `job_ticks` by `status`, `type` |
| Is `run_retries.max_retries = 3` masking a chronic failure? | retry-tagged run counts |
| Is the event log growing unboundedly? | table/index sizes via `pg_relation_size` |

Two constraints on the implementation: every query must be time-bounded and index-aware
(`runs` and `event_logs` are large), and the exporter should connect **directly to RDS**,
not through PgBouncer, so its polling doesn't contaminate the pool metrics we're
collecting in parallel.

#### Shipped 2026-08-17 — `burningalchemist/sql_exporter`, one Deployment in `dagster`

Nine gauges, all under a 10s `statement_timeout` and a 2-connection ceiling:

| Metric | Answers |
|---|---|
| `dagster_runs_in_flight{status}` | is `max_concurrent_runs` binding |
| `dagster_oldest_queued_run_age_seconds` | healthy burst vs. stuck coordinator |
| `dagster_run_wait_to_start_seconds{quantile}` | p50 / p95 / max wait to start |
| `dagster_recent_runs{status}` | failure rate over the last 6h |
| `dagster_recent_retried_runs` | chronic failure masked by `max_retries = 3` |
| `dagster_recent_job_ticks{tick_type,status}` | silently erroring sensors/schedules, last 1h |
| `dagster_daemon_heartbeat_age_seconds{daemon_type}` | the 08-10 symptom, measured directly |
| `dagster_id_window_span_seconds{relation}` | which bound is binding (see below) |
| `dagster_relation_bytes{relation}` | event-log growth |

Three implementation findings worth keeping, all from testing rather than reading:

- **Every window is bounded by time; how that is made cheap depends on the metric.** The
  rule that came out of two rounds of getting this wrong: *a cost bound may never cut
  across the dimension the metric is defined on.*

  The first pass was id-only, sized against an assumed ~53k runs/day that is wrong by more
  than an order of magnitude. Measured from kube-state-metrics over the seven days to
  2026-08-18: ~143 runs/hour on a quiet day, ~484/hour averaged over the week, ~1,990 in
  the busiest hour, ~7,416 in the busiest six. A 14x swing — so a fixed 20,000-id window
  covered anywhere from ~10 hours to ~6 days depending on when you looked, and
  `dagster_recent_runs` reported a six-day trailing count while calling itself recent.

  Those seven days are now known to be the retry storm below, so every figure in that
  paragraph is inflated: steady state is ~520–670 runs/day, not ~3.4k. It does not change
  what #5495 did — the id cap was over-provisioned then and is more over-provisioned now —
  but it does mean the sizing has never been derived from a quiet week.

  Adding a lookback fixed the span and introduced a subtler fault, caught in review: `id`
  is **creation** order, and the completion-time metrics filter on `update_timestamp`. Any
  run created before the cap but finishing inside the lookback was silently dropped —
  long-running and long-queued runs first, which are exactly the ones worth counting.
  Demonstrated on the fixture with a run created nine days ago that finished five minutes
  ago: the id-capped query returns 0, the corrected one returns 1.

  So the cost bound now follows what the metric measures:

  - **Completion-time metrics** (`dagster_recent_runs`, `dagster_recent_retried_runs`)
    carry **no id cap at all**. `idx_run_range` is `(status, update_timestamp,
    create_timestamp)`, so pairing the status predicate they already need with an
    `update_timestamp` range makes them index-only range scans whose cost tracks
    rows-in-window rather than table size. This is the exception to "no index on a bare
    timestamp" — there isn't one, but there is a usable composite index the moment status
    is pinned. Faster as well as correct: **0.3ms against 4.6ms**, and even a deliberately
    pathological 30-day lookback stays a range scan (48k rows, 9.4ms).
  - **Creation-ordered metrics** keep the id cap because it agrees with them.
    `dagster_run_wait_to_start_seconds` is a cohort of recently *created* runs — there is
    no index on `start_time`, so it can't be driven off an event-time index — and
    `job_ticks.id` tracks tick timestamp. Cap and predicate move together, so nothing
    falls between them. The stated limitation, in the metric's own `help`: a run created
    just before the window and starting just inside it isn't counted.

  `SQL_EXPORTER_RUN_WINDOW = 20000` (2.7x headroom over the busiest 6h in 30 days),
  `SQL_EXPORTER_TICK_WINDOW = 8000` with a 1h lookback — shorter lookback because the
  daemon evaluates every sensor on a ~30s cadence whether or not it yields a run, and
  12x headroom over the measured peak tick rate of 651/hour. Both are sized against the
  peak, never the quiet week; see *The id caps are sized against the peak* below.

  `dagster_id_window_span_seconds{relation}` reports whether those remaining id caps are
  truncating: span well above the lookback means the time predicate binds; span at or
  below it means the cap does and wants raising. It covers **only** the two id-capped
  windows and says so. An earlier revision claimed to cover the completion-time metrics
  too and could not — it measured creation age against a completion-time filter, so a
  healthy-looking span would have sat happily beside a metric dropping every long-running
  run. That is the same class of mistake as the one the gauge exists to catch, one level up.
- **`SQLEXPORTER_TARGET_DSN` is only read when the config file declares no `target:` block
  at all.** A `target:` with `data_source_name` omitted does not fall back to the
  environment — it fails at startup with `missing data_source_name for target`. So the
  ConfigMap carries collectors only and the target is entirely environment-driven, which
  is also what keeps the Vault credential out of the ConfigMap.
- **`statement_timeout` must use the libpq `--name=value` form**:
  `options=--statement_timeout%3D10000`. The more familiar `-c name=value` form is mangled
  in transit and the server rejects it with
  `invalid command-line argument for server process: -c`. Confirmed the timeout actually
  reaches the backend by setting it to 1ms and watching every query return `57014`.

The three window metrics carry **no `_total` suffix**. They are gauges over a sliding id
window and fall as runs leave it; `_total` announces a monotonic counter, which invites
`rate()` and makes every ordinary decrease look like a counter reset.

The exporter authenticates with `postgres-dagster/creds/readonly` — SELECT and nothing
more, which covers even `pg_total_relation_size`, verified by running the whole collector
under a role holding only SELECT. `dagster_server_policy.hcl` had to be widened; it
granted `creds/app` only.

**`OLVaultK8SSecret` never renders `spec.revoke`, so no dynamic secret in this repo
revokes its lease on deletion.** Found while reviewing this PR. `revoke_on_delete=True`
and `role_name=...` are passed at several call sites (including the pre-existing
`dagster_db_secret`) but are not fields on the config model, so Pydantic drops them
silently — the intent reads as expressed and none of it is in effect. `refresh_after` is
a real field but is only rendered for *static* secrets, and would be overridden by the
lease duration anyway. On `postgres-dagster` that lease is 3 months
(`OLVaultDatabaseConfig.default_ttl = ONE_MONTH_SECONDS * 3`), so a deleted
VaultDynamicSecret leaves working database credentials behind for up to a quarter. VSO
1.5.1 does support `spec.revoke`; the component simply does not emit it. Repo-wide fix,
tracked separately.

One deliberate omission from the table above: per-code-location failure rate. The join
from `runs` to `run_tags` for every run in the window is the most expensive query in the
set, and the kube-state-metrics label allowlist in §5 answers a near-enough version of
the same question for free. Revisit if the two ever disagree — they measure different
things (Dagster run status vs. run-worker Job exit) and a systematic gap between them
would itself be worth knowing about.

#### What the first scrape found: a 95% failure rate the cluster had been calling healthy

The gap the paragraph above says "would itself be worth knowing about" opened on the very
first scrape, 2026-08-18 16:39Z, before the time bound in #5495 had shipped:

```
dagster_recent_runs{status="FAILURE"}   18973
dagster_recent_runs{status="SUCCESS"}    1023
dagster_recent_retried_runs             14680
dagster_recent_job_ticks{tick_type="SENSOR",status="FAILURE"}  1020
```

Meanwhile `increase(kube_job_status_failed{namespace="dagster"}[7d])` was ~21. Both were
right. A Dagster run that fails cleanly — an op raises, the failure is recorded — still
lets the run worker exit 0, so the k8s Job counts as SUCCEEDED. `kube_job_status_failed`
sees infrastructure failures (OOM, eviction, crash) and is blind to application ones. That
is the entire blind spot this exporter was built to close, and it closed it immediately.

**The failures were real.** Confirmed independently in Loki, which does not go through the
exporter and did not exist to serve it. Counting `RUN_FAILURE` lines on `data-production`:

| Window (UTC, `count_over_time[6h]`) | Failed runs |
|---|---|
| 08-08 00:00 → 08-11 12:00 | 2–350 per 6h (one 3,659 spike in the 08-07 18:00 bucket) |
| 08-11 12:00 → 18:00 | 8,739 — burst begins |
| 08-12 18:00 → 08-13 00:00 | 27,861 — peak |
| sustained 08-11 → 08-18 | 8k–28k per 6h |
| 08-18 03:30 onward | 0–40 per 6h |

Every one of them is the same asset: step `extract_edxorg_courserun_metadata` in the
`edxorg` code location, failing `__ASSET_JOB`, each line carrying a distinct run id.

**The first suspect was wrong, and wrong in an instructive way.** `learning-resources`
held 211 of the 221 Jobs in the namespace, an order of magnitude more than any other
location, which looked like a job failing and relaunching in a loop. It was volume, not
failure. Job count is a proxy for neither.

**Root cause, and it is already fixed.** `upstream_or_code_changes()` in
`ol_orchestrate.lib.automation_policies` ORed in a bare `execution_failed()`. That term is
*level*-triggered: a partition that can never succeed reads true on every sensor tick
forever, and `run_retries.max_retries = 3` multiplied each re-request by four. A code
deploy is what starts it — `dagster-edxorg:dcce1f1b` rolled 2026-08-11T13:03Z, inside the
six-hour bucket the burst begins in — because a changed code version re-requests every
partition; the asset then fails, and the level-triggered term never lets it stop.
ol-data-platform #2564 changed the term to `.newly_true()`, which spends one failure on
one re-request. `dagster-edxorg:12bc637e` rolled 2026-08-17T21:36Z.

**The tail confirms the mechanism.** The burst does not stop at the 21:36Z deploy; it
drains over the following five hours — 706 failures in the 02:00Z half-hour, 467 at 02:30,
208 at 03:00, 0 at 03:30. That is exactly what #2564's own deploy note predicts:
`NewlyTrueCondition` diffs against a cursor that does not exist on its first evaluation,
so every already-failed partition reads as newly true once. One final wave, four retries
each, then silence.

**Current state, 2026-08-24**, all three of the affected metrics back to a normal shape:

| Metric | During the storm | Now |
|---|---|---|
| failure share of terminal runs | ~95% | 5–15% per 6h (7–28 failures against 137–222 successes) |
| `dagster_recent_retried_runs` | 14,680 of 20,000 (73%) | 6–21 per 6h |
| sensor ticks in `FAILURE` | 1,020 | 0–1 per hour |

**Every run-rate figure this project has carried was measured inside the storm.** The
~53k/day of the original design was wrong; the ~3.4k/day that replaced it in #5495 was
measured on 2026-08-18, still inside the tail, and is wrong too.
`increase(kube_job_status_succeeded{namespace="dagster"}[24h])` per day since:

```
08-17  9179     08-21   616
08-18  3552     08-22   564
08-19  1012     08-23   521
08-20   674     08-24   528
```

Steady state is **~520–670 runs/day**, corroborated independently by the exporter's own
6h `SUCCESS` counts (137–222, i.e. ~550–890/day). A 17x collapse from the figure the
windows were sized against. Nothing is broken by this — `dagster_id_window_span_seconds`
reports the `runs` cap now spanning 6.8 days and `job_ticks` 3.6 days against lookbacks of
6h and 1h, so both caps are non-binding by a wide margin and the time predicates are doing
all the work. Stated as ratios rather than lumped together, because they are not the same
number: the `runs` cap overshoots its lookback by ~27x (6.8 days against 6h) and the
`job_ticks` cap by ~85x (3.6 days against 1h). Both cost a wider scan and nothing else.

#### The id caps are sized against the peak

Re-sizing them from the quiet week, which is what this correction seemed to call for, is
the wrong move and was rejected. An id cap's failure mode is *silent truncation*, and it
can only truncate under load, so a cap tuned to steady state is guaranteed to bind exactly
when the metric it guards is worth reading. Over-provisioning costs a wider index scan and
nothing else. The two directions are not symmetric and should not be traded off as though
they were.

The full 30-day series — `sum(increase(kube_job_status_succeeded{namespace="dagster"}[6h]))`
at 6h resolution, 2026-07-25 to 2026-08-24 — puts numbers on it:

| | runs per 6h |
|---|---|
| quiet, both before the storm and since | 96–230 |
| storm, sustained | 1200–3500 |
| storm, peak | **7416** |

A 50x swing on the same workload. `SQL_EXPORTER_RUN_WINDOW` stays at **20000**, 2.7x over
that peak. Cutting it to the ~2000 a quiet week suggests would have truncated **30 of the
42 six-hour buckets** in that stretch, and `dagster_run_wait_to_start_seconds` is a
queue-depth metric — a storm is the only time anyone reads it.

`SQL_EXPORTER_TICK_WINDOW` is the one that genuinely overshoots, and it overshoots against
its own peak rather than against a quiet week, so the rule above does not protect it. Ticks
are cadence-bound, not demand-bound: tick rate tracks how many sensors exist, not how much
work they find. Measured hourly from `dagster_recent_job_ticks` since #5495: on Production
230–651, stepping to a flat 605–651 at 2026-08-22 21:29Z when the sensor set changed; on QA
a flat 455–462 since 2026-08-19. Peak 651/hour against a 1h lookback made 40000 a 61x cap. Cut to **8000**, which is
12x the higher of the two environments, and still holds the span above the lookback if the
sensor set grows eightfold.

One measurement trap worth recording, since it nearly set this cap too low in the other
direction: `max_over_time(sum(dagster_recent_job_ticks{cluster="data-qa"})[7d])` returns
**12983**, which would have argued against 8000. Those samples predate the #5495 rollout to
QA, when the query had no lookback at all and the metric was a whole-table count rather
than an hourly rate. Any `dagster_recent_*` series from before #5495 landed on a given
cluster measures a different thing under the same name.

#### The storm started four days earlier than recorded

Third correction from the same series: run creation steps from ~120 to ~2136 per 6h in the
bucket covering **2026-08-07 17:12Z–23:12Z**, not on 2026-08-11, and has fallen back to
~112 by the bucket ending 2026-08-18 11:12Z — consistent with the 03:30Z end already on
record. The end date on record is right; the start was taken from when the symptom was
noticed rather than when the rate moved. Any window opened on 08-11 to avoid
the storm still contains three days of it — which matters most for
`tk-set-alert-thresholds-on-the-new-dagster-sql-expo-0063d0`, where a contaminated
baseline becomes a threshold.


### 4. OpenTelemetry auto-instrumentation — the "why", not the "what"

Worth evaluating separately from the SQL exporter, because the two answer different
questions and neither substitutes for the other.

**The receiving side already exists.** `grafana-k8s-monitoring-alloy-receiver` on
`data-production` serves OTLP on 4317/4318 alongside a tail-sampling collector, and the
house env-var pattern is already set by `mit_learn`, `learn_ai`, `witan` and
`toolhive_telemetry.py`: `http/protobuf` to port 4318,
`OTEL_TRACES_SAMPLER=parentbased_traceidratio` at `0.25`. So this is env vars plus an
image entrypoint change — no new infrastructure.

**What auto-instrumentation would hook.** `opentelemetry-bootstrap` against a
`dagster==1.13.17` environment selects instrumentation for sqlalchemy, psycopg2, grpc,
botocore, requests, urllib3, aiohttp, asyncio, threading and logging — which is Dagster's
entire I/O surface:

| Span source | What it would answer |
|---|---|
| SQLAlchemy `connect` (CLIENT span) | The per-query TCP + SCRAM handshake cost against PgBouncer — the direct price of `NullPool` |
| SQLAlchemy cursor execute | Which Dagster component issues which queries, and how slow they are |
| gRPC client/server | Webserver and daemon → code location call latency; whether `DAGSTER_GRPC_TIMEOUT_SECONDS = 300` and `DAGSTER_CODE_SERVER_TIMEOUT_SECONDS = 120` are anywhere near reality |
| botocore | S3 compute log manager and I/O manager latency inside long runs |
| Trace propagation | The daemon → run worker → code location hop as one trace |

The `connect` span is the interesting one. Confirmed directly from `dagster_postgres`
source, the engine is built as
`create_engine(postgres_url, isolation_level="AUTOCOMMIT", poolclass=db_pool.NullPool)` —
so every query really is a fresh connection, and the `connect` span prices that tax
per-query. That is the number that decides whether moving to `transaction` pool mode or
introducing a real client-side pool is worth the work. Nothing else we can collect
measures it.

**What it explicitly does not buy.** Auto-instrumentation is library-level: it has no
concept of a Dagster run. It yields nothing on run status, queue depth, sensor and
schedule tick outcomes, retry attribution, materializations, or backfills. There is no
Dagster instrumentation package on PyPI — `opentelemetry-instrumentation-dagster`,
`dagster-opentelemetry` and `dagster-otel` all 404 — and no first-party OTel support.

The metrics side is also thin. The only database metric the SQLAlchemy instrumentation
emits is `db.client.connections.usage`, an up-down counter tagged `idle`/`used` driven by
pool checkout/checkin events. Under `NullPool` that oscillates between 0 and 1 and tells
us nothing about pool sizing. **The value here is in traces, not metrics** — which is
exactly why this complements rather than replaces the SQL exporter and the PgBouncer
exporter.

**Costs specific to this deployment.** Run workers are the problem case. Measured on
`data-production`: ~2 200 Job objects at a one-hour TTL (≈53 000 runs/day), with
individual run workers living **7–9 seconds**.

- *Startup tax* — benchmarked `import dagster, dagster_postgres, dagster_k8s` at 1.55 s
  baseline vs 1.60–1.68 s under `opentelemetry-instrument` (1.73–1.94 s with an OTLP
  exporter configured). That is ~0.1–0.2 s, roughly 2–3% of an 8-second run. Tolerable.
- *Export reliability* — `BatchSpanProcessor` defaults to a 5 s schedule delay, so an
  8-second process relies entirely on the at-exit flush. A SIGKILL loses the spans, and an
  unreachable collector makes shutdown retry block process exit.
- *Volume* — 756 queries/s measured through PgBouncer, and `NullPool` means a `connect`
  span *plus* a query span for each. That is ~1 500 DB spans/s before gRPC, S3 or root
  spans. Even at the house 0.25 ratio it lands around 400 spans/s, on the order of
  10⁷–10⁸ spans/day. This needs a deliberate sampling decision, very likely a lower ratio
  for run workers than for the control plane.
- *Double instrumentation* — bootstrap selects sqlalchemy **and** psycopg2, which would
  emit two spans per query. Needs `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=psycopg2`.
- *Ownership* — the entrypoint change lives in `ol-data-platform`, not this repo. Only the
  env vars belong in `__main__.py`.

**Suggested scope.** Instrument the long-lived control plane first — webserver, daemon and
the ten code location deployments. Those are stable processes where span volume is
bounded, the at-exit flush problem doesn't exist, and the gRPC and `connect` spans answer
questions we currently guess at. Defer run workers until the sampling ratio is chosen
against observed volume.

### 5. Free adjacent win — kube-state-metrics label allowlist

Dagster run Jobs already carry `dagster/code-location`, `dagster/job`, and
`dagster/run-id` labels. KSM is deployed
(`grafana.py:501`) but without `--metric-labels-allowlist`, so `kube_job_labels` is not
emitted and the ~2000 `kube_job_*` series in the namespace are anonymous — we can count
run-worker successes and failures but cannot break them down.

Allowlisting `jobs=[dagster/code-location,dagster/job]` would give per-code-location run
outcomes with no new deployment. **`dagster/run-id` must be excluded** — it is unique per
run and would be a cardinality bomb.

**The passthrough path is now confirmed** against chart 4.3.2 (`helm pull` + read the
subchart values, since these keys are `@ignored` in the published `values.yaml` and so do
not appear in the rendered docs):

- `k8s-monitoring` declares `telemetry-services` as a dependency aliased to
  `telemetryServices`, and `telemetry-services` in turn depends on
  `kube-state-metrics` 8.0.0. So the key is
  **`telemetryServices.kube-state-metrics.metricLabelsAllowlist`**, a plain list rendered
  into KSM's `--metric-labels-allowlist` flag.
- **It already has a non-empty default** — a long `nodes=[…]` entry covering instance
  type, nodegroup, zone, etc. Helm *replaces* lists rather than merging them, so the
  `nodes=[…]` entry must be copied verbatim into our value or we silently drop every
  node label the cluster dashboards depend on. This is the trap in this change.
- Downstream is clear: `feature-cluster-metrics/default-allow-lists/kube-state-metrics.yaml`
  keeps `kube_job.*`, so `kube_job_labels` passes the Alloy metric filter and reaches
  Grafana Cloud without any further tuning.

This overlaps with what the SQL exporter provides and is strictly less rich (no queue
time, no retry attribution), so treat it as a cheap stopgap rather than a substitute.

✅ Done 2026-08-17. Re-confirmed against chart **4.4.0** (the version above was 4.3.2):
the path and the list-replacement trap both hold, `kube-state-metrics` is now 8.1.3, and
rendering the chart shows
`--metric-labels-allowlist=nodes=[…],jobs=[dagster/code-location,dagster/job]`. The
premise was checked live as well — all 829 Jobs in the `dagster` namespace on
data-production carry `dagster/code-location`, `dagster/job` and `dagster/run-id`.

## Suggested sequencing

0. **Cap the aggregate connection count.** Set `max_db_connections` so that
   `max_db_connections × pgbouncer_replica_count` lands safely under
   `max_connections = 5000` (≈700 at 6 replicas). This is the only item here that makes a
   repeat of 08-10 structurally impossible, it needs no telemetry to justify, and it is a
   one-line ConfigMap change. Everything else on this list measures the problem; this one
   removes it.
1. **Cut the PgBouncer log volume.** Set `log_connections = 0` / `log_disconnections = 0`.
   These lines have negative value at this churn rate — they cost real Loki ingest and
   bury the one useful line per minute. Do this regardless of everything below. Ships in
   the same ConfigMap edit as (0), so the two should be one PR.
2. **PgBouncer exporter sidecar + ServiceMonitor.** Lowest effort, highest information
   density, and the pipeline is already there.
3. **Alert on connection headroom.** `sum(pgbouncer_pools_server_*) / 5000` with a burn
   threshold. This is the alert that would have fired on 08-10.
4. **Re-enable Performance Insights** on `ol-etl-db-production`. ✅ Done 2026-08-17 —
   `performance_insights_enabled = True`, gated to the production stack, retention
   inherited at the free 7 days. `pulumi preview` confirmed an in-place update
   (`performanceInsightsEnabled: false => true`), no replacement and no reboot.
   Enhanced Monitoring and the CloudWatch alarm profile stay off deliberately; see the
   comments at the override site for why. Enabling the alarm profile is its own
   decision because none of its thresholds have been checked against this instance —
   not, as an earlier draft of this line claimed, because the alarms omit `ok_actions`;
   `OLCloudWatchAlarmSimpleRDS` sets `ok_actions=alarm_actions`
   (`components/aws/cloudwatch.py:211-212`), so they do auto-resolve in Rootly.
5. **Dagster SQL exporter.** ✅ Done 2026-08-17 — see
   [Shipped](#shipped-2026-08-17--burningalchemistsql_exporter-one-deployment-in-dagster).
   It connects directly to RDS and so consumes from the same 5000, which is already
   budgeted: `DB_CONNECTION_HEADROOM_FACTOR = 0.85` leaves ~750 connections outside the
   pool's cap and the exporter takes 2 of them.
6. **Re-tune from data.** ✅ Done 2026-08-17 — see
   [The answers](#the-answers-2026-08-17-seven-days-of-1-minute-samples-on-production).
   `min_pool_size` 150 → 40, `default_pool_size` → the derived cap, `reserve_pool_size`
   → 0. Replica count deliberately held at 6 so this change is attributable; it is the
   next lever.

## Open question worth flagging

See also [`dagster-connection-backpressure.md`](./dagster-connection-backpressure.md),
which follows the `NullPool` thread below to its conclusion. The short version: the fix
for it is already written and already installed in the production image
(`ol_orchestrate.lib.postgres.PooledPostgres*Storage`, which swap `NullPool` for
`QueuePool`) and simply not switched on — but the defaults would be catastrophic if
deployed as-is, so it needs the exporter's numbers first.

`pool_mode = session` combined with Dagster's `NullPool` means PgBouncer provides almost
no multiplexing — each client connection holds a server connection for its whole
(sub-second) session, so the ratio of client to server connections stays near 1:1. The
benefit we get today is amortised TCP + SCRAM setup against RDS, not connection
reduction. Whether `transaction` mode is viable is a separate question
(`max_prepared_statements = 0` is already set, which is the usual blocker), but the
metrics above are the prerequisite for answering it either way — and the answer would
change every pool-sizing number in the file.
