# Can PgBouncer pool capacity drive backpressure on the Dagster daemon?

Investigation on 2026-08-14 against `data-production`, `dagster==1.13.17`, and the
`ol-data-platform` checkout. Companion to
[`dagster-pgbouncer-observability.md`](./dagster-pgbouncer-observability.md).

## Short answer

Yes, mechanically — Dagster has a runtime-mutable concurrency limit that gates run
*launch*, so a controller reading PgBouncer metrics could throttle the daemon. But it
should not be the next thing we build, for two reasons:

1. **It controls the wrong variable.** Connection count is not a function of run count;
   under `NullPool` it is a function of *query concurrency*, and the transfer function
   between them is neither stable nor knowable. On 08-10 the ratio happened to be
   ~28 connections per run worker (4989 / 178), but nothing holds that constant.
2. **The direct fix is already written, already installed in the running image, and not
   switched on.** `ol-data-platform` ships `PooledPostgresRunStorage`,
   `PooledPostgresEventLogStorage` and `PooledPostgresScheduleStorage`, which replace
   `NullPool` with `QueuePool`. That makes each process's connection count *bounded by
   configuration* rather than unbounded in query rate — which turns the whole problem
   from a control loop into arithmetic.

Do the arithmetic first. Revisit the control loop only if the numbers don't close.

## What Dagster actually exposes

Read from the installed `dagster==1.13.17`, not from the docs.

| Lever | Runtime-mutable? | Gates run launch? | Verdict |
|---|---|---|---|
| `max_concurrent_runs` (currently 100) | **No** — dagster.yaml, daemon restart | Yes | Static bound only |
| `tag_concurrency_limits` | No — dagster.yaml, daemon restart | Yes | Static bound only |
| Concurrency pools (`set_concurrency_slots`) | **Yes** — DB-backed, no restart | **Yes**, see below | The only dynamic lever |

The pool limit is the interesting one, and there are three ways to write it, all landing
on the same `concurrency_limits` table:

- `instance.event_log_storage.set_concurrency_slots(key, num)` (Python)
- `setConcurrencyLimit` GraphQL mutation (`dagster_graphql/schema/roots/mutation.py:1126`)
- `dagster instance concurrency set <key> <limit>` (CLI)

**Pool limits gate run launch, not just op execution.** `RunQueueConfig` carries
`should_block_op_concurrency_limited_runs`, which defaults to `True` and is unset in our
`dagster_instance.yaml`, so it is in effect. The queued run coordinator daemon keeps a
`_global_concurrency_blocked_runs` set and declines to dequeue runs whose root ops are
pool-limited. So writing a smaller pool limit really does stop new run workers from being
created — which is the behaviour a backpressure loop needs.

One dead end worth recording so nobody re-checks it: `RunQueueConfig` also has a
`with_concurrency_settings()` method that reads a `runs.max_concurrent_runs` override,
which looks exactly like a runtime hook for the thing we'd want to tune. It is never
called anywhere in OSS Dagster — it is a Dagster+ code path. `max_concurrent_runs` cannot
be changed without restarting the daemon.

## What a closed loop would cost

To build the loop we would need all of:

1. **Pools declared on assets/ops in `ol-data-platform`.** There are currently none — a
   search for `pool=` across the repo returns nothing. Without a pool key on the root op,
   `should_block_op_concurrency_limited_runs` has nothing to grab and the lever is inert.
   This is the largest piece of work, and it lands in the other repo.
2. **A controller** — something scraping `pgbouncer_pools_server_active_connections` and
   writing `setConcurrencyLimit`. New deployment, new failure mode, new thing to alert on.
3. **A stable control law.** This is the part that worries me most. Run workers live
   **7–9 seconds**. A loop that samples a 30s-interval metric to throttle a 8-second
   process is sampling well below the Nyquist rate of the thing it controls; the standard
   outcome is oscillation — throttle hard on a stale reading, starve the queue, release,
   overshoot. The 08-10 aftershock trace (hours of 2400↔4900 swings after the plateau
   cleared) is what uncontrolled oscillation already looks like here; a badly tuned
   controller would reproduce it deliberately.

## Why the arithmetic path is better

`dagster_postgres` builds its engine as
`create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)`. Confirmed live in
the production image. Under `NullPool` there is **no per-process bound at all** — a
process running 28 concurrent queries opens 28 connections, and nothing stops it opening
200. Total connections are therefore `f(query concurrency)` across every process, which
is exactly the unbounded quantity that reached 4989.

`QueuePool` changes the shape: per-process connections are capped at
`pool_size + max_overflow`. Total becomes `processes × per-process cap` — a number you can
compute in advance and check against the budget.

**The classes to do this already exist and are already deployed.** Verified by importing
them inside the running daemon pod:

```
$ kubectl -n dagster exec dagster-daemon-… -- python -c \
    "from ol_orchestrate.lib.postgres import PooledPostgresEventLogStorage; ..."
importable in the deployed image: ol_orchestrate.lib.postgres.event_log
stock uses NullPool: True
dagster 1.13.17
```

They are referenced in three `ol-data-platform` docs and used by nothing. The deployed
`dagster.yaml` — read out of the running daemon — still names the stock classes for all
three storages. **Switching them on is a `dagster_instance.yaml` edit in this repo. No
code change, no image rebuild.**

### The sizing trap — do not deploy the defaults as-is

This is almost certainly why it was written and never switched on, and it needs to be
settled before anyone flips it.

`DagsterInstance` builds **three** storages (run, event log, schedule), and the pooled
classes give **each one its own engine** with defaults `pool_size=10, max_overflow=20`.
That is up to **90 connections per process**, with a **30-connection persistent floor**.

Process count on `data-production` today:

| | Count |
|---|---|
| daemon | 1 |
| webserver | 2 |
| code location replicas | 14 |
| **long-lived subtotal** | **17** |
| run workers (bounded by `max_concurrent_runs`) | up to 100 |
| **total** | **up to 117** |

Run workers mount the same `dagster-instance` ConfigMap, so they get the same pool config.
At the defaults that is a **510-connection persistent floor and a 10,530 peak** — twice as
bad as the failure we are fixing, and it would sit *below* the PgBouncer cap, so
`max_db_connections` would convert it into permanent queueing rather than an outage.

Sized against the new 4200-connection aggregate budget, the constraint is:

```
117 processes x 3 storages x (pool_size + max_overflow) <= 4200
  =>  pool_size + max_overflow <= ~11
```

So something like `pool_size=3, max_overflow=8` (33/process peak, 9 floor). For reference
the daemon currently holds **21 concurrent connections** at steady state, so a 33-connection
ceiling is comfortable for it. The right numbers should be set from the exporter's
`pgbouncer_pools_server_active_connections` once it is collecting, not from this estimate.

Better still would be a smaller pool for run workers than for the control plane — a
7-second process has no use for a warm pool of 10 — but the two share one ConfigMap today,
so that needs a separate `dagster.yaml` for the run launcher and is a follow-on, not a
prerequisite.

## Recommended sequence

1. **`max_db_connections` cap** — done, shipped with the exporter. The hard safety net;
   nothing below can cause an outage while it holds.
2. **PgBouncer exporter** — done. Gives the per-process connection numbers that steps 3
   and 4 need to be set from data rather than from the estimate above.
3. **Switch on the pooled storage classes**, with `pool_size`/`max_overflow` sized from
   what step 2 measures. This is the actual fix: it converts connection count from
   unbounded-in-query-rate to bounded-by-config.
4. **Re-derive `max_concurrent_runs` from arithmetic** once per-process connections are
   bounded. At 33/process the current 100 is comfortably inside budget; the number stops
   being a guess.
5. **Only then** consider pools + a controller — and only if measurement shows a real
   dynamic range that a static bound can't cover. Given a bounded per-process cost and a
   hard PgBouncer ceiling underneath it, I expect it won't be needed.

## Adjacent finding

The daemon holds 21 of the 25 client connections at steady state; the two webservers hold
2 each; run workers hold none when idle. Any per-process pool sizing should treat the
daemon as the outlier it is, rather than assuming uniform cost across the 17 control-plane
processes.
