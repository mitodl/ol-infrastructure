# Granian configuration overhaul

**Status:** stage 0 (component change) implemented 2026-07-22; stages 1–4 pending
**Project:** `wp-granian-configuration-overhaul-expose-blocking-t-3debc2`
**Component:** `src/ol_infrastructure/components/services/k8s.py` — `GranianConfig`
**Evidence:** witan lessons `les-granianconfig-never-exposes-blocking-threads-bac-874462`,
`les-ceiling-based-granian-workers-max-rss-mitxonline-2136f0`

## Problem

`GranianConfig` exposes no `blocking_threads` or `backpressure` field, so `build_args()`
never emits `--blocking-threads` / `--backpressure` for any caller. Granian resolves them
itself (`granian/server/common.py:155-166`):

```
backpressure     = max(1, backpressure_arg or backlog // workers)
blocking_threads = blocking_threads_arg or (max(1, backpressure // 2) if WSGI else 1)
```

With the component defaults `backlog=128`, `workers=2`, `interface="wsgi"`:

```
backpressure     = 128 // 2 = 64
blocking_threads = 64 // 2  = 32 per worker  →  64 GIL-competing Python threads per pod
```

Granian itself warns when `blocking_threads > cpu_count() * 2 + 1`
(`granian/server/mp.py:445-450`). No pod here is near the ~15 vCPU that would make 32
defensible — webapp CPU requests run 100m–500m with no CPU limit. This matches the
"more concurrency, worse latency" GIL-contention pattern reported in Granian
discussion #663.

Three secondary deviations from Granian's own defaults compound it:

| Setting | Granian CLI default | Component default | Maintainer guidance |
|---|---|---|---|
| `workers` | 1 | **2** | "1 worker per pod, scale via replicas" (#406), "1–2" (#663) |
| `runtime_threads` | 1 | **2** | "leave threading at default" (#663) |
| `runtime_mode` | auto | **"mt"** | same |

And `--workers-max-rss` sizing: mitxonline derives it from the VPA *ceiling* (3Gi) rather
than the pod's current declared limit (1200Mi), so two workers can reach ~2816MiB before
the cap fires — more than 2x the actual cgroup limit in the steady state. The kernel OOM
killer wins the race, which is exactly the failure the RSS cap exists to prevent.

## Affected callers

Eight webapp deployments across seven Pulumi projects. Note `edxapp` (LMS + CMS) was not
in the original finding but is affected identically.

| App | interface | workers | backlog | eff. blocking_threads | min_replicas | mem limit |
|---|---|---|---|---|---|---|
| `edxapp` LMS | wsgi | 2 | 128 | **32** | stack config | stack config |
| `edxapp` CMS | wsgi | 2 | 128 | **32** | stack config | stack config |
| `micromasters` | wsgi | 2 | 128 | **32** | 2 | 2000Mi |
| `mitxonline` | wsgi | 2 | 128 | **32** | 2 | 1200Mi (VPA→3Gi) |
| `ocw_studio` | wsgi | 2 | 128 | **32** | 2 | 3Gi |
| `odl_video_service` | wsgi | 2 | 128 | **32** | 2 | 1Gi |
| `xpro` | wsgi | 2 | 128 | **32** | 2 | 2Gi |
| `mit_learn` | asginl | 2 | None | 1 (async) | 2 | 3200Mi |
| `learn_ai` | asgi | **1** | None | 1 (async) | 2 | 1000Mi |

`mit_learn` / `learn_ai` are unaffected on the blocking-threads axis (async interfaces
force `blocking_threads=1`) but still get an untuned `backpressure` of 1024 from
Granian's default backlog. `learn_ai` already models the target shape: `workers=1`,
`runtime_mode=None`.

## Component changes

All in `GranianConfig` and the synth-time block at `k8s.py:855-878`.

### 1. New field: `blocking_threads`

```python
blocking_threads: PositiveInt | None = None
"""Size of the Python thread pool that executes WSGI request handlers
(granian --blocking-threads). These threads compete for the GIL, so this is a
concurrency knob, not a throughput knob -- keep it within a small multiple of the
container's CPU allocation. Only meaningful for interface='wsgi'; Granian forces it
to 1 for asgi/asginl. When None on a WSGI app, resolves to
DEFAULT_WSGI_BLOCKING_THREADS rather than letting Granian derive it from
backpressure//2."""
```

- Resolution happens in a `model_validator(mode="after")`, not at `build_args()` time, so
  the effective value is inspectable and testable.
- WSGI + `None` → `8`. asgi/asginl + `None` → not emitted (Granian's forced 1).
- asgi/asginl + explicit value `> 1` → **`ValueError`**. Granian silently ignores it;
  failing at synth time is strictly better than a config that reads as tuned but isn't.
- `build_args()` emits `--blocking-threads <n>` only when the resolved value is not None.

### 2. New field: `backpressure`

```python
backpressure: PositiveInt | None = None
"""Maximum in-flight requests a single worker will accept before it stops draining the
accept queue (granian --backpressure). Excess connections wait in the kernel backlog
(and behind the nginx sidecar), which is where they belong -- an oversized backpressure
just moves the queue inside the worker where it inflates tail latency invisibly. When
None on a WSGI app, resolves to 2x the resolved blocking_threads."""
```

- WSGI + `None` → `2 * resolved_blocking_threads` (so `16` at the new defaults).
- asgi/asginl + `None` → not emitted; Granian's `backlog // workers` stands. This is a
  deliberate no-op for `mit_learn` / `learn_ai` in this change.
- `build_args()` emits `--backpressure <n>` when resolved.

### 3. Default changes

| Field | Old | New | Note |
|---|---|---|---|
| `workers` | 2 | **1** | Granian CLI default; maintainer K8s guidance |
| `runtime_threads` | 2 | **1** | Granian CLI default |
| `runtime_mode` | `"mt"` | **`None`** | Granian's "auto"; `build_args()` already omits on None |

`backlog` stays at 128. Once `backpressure` is explicit, `backlog` no longer feeds the
thread-pool derivation and reverts to meaning only what it says: the listen backlog.

### 4. `workers_max_rss` sizing

Keep the mechanism and keep the existing synth-time formula
`floor(resource_limits["memory"] * 0.9 / workers)`, evaluated against the pod's **current
declared limit**. With `workers=1` this is simply 90% of the container limit, leaving 10%
for the Granian master and interpreter overhead.

Decided against the runtime cgroup-read variant (an entrypoint wrapper reading
`/sys/fs/cgroup/memory.max`) for this project: it is a cross-repo image change, and the
lesson's own caution about `mit_learn_nextjs`'s cgroup auto-sizing landing at a
surprisingly low value in production applies. **Tracked as a follow-on task** to evaluate
after the concurrency changes are validated in production.

Consequences:

- `mitxonline`'s ceiling-derived override (`__main__.py:536-543, 563-569`) is **removed**,
  along with `MITXONLINE_GRANIAN_WORKERS` and `GRANIAN_MASTER_OVERHEAD_MIB`. It reverts to
  the component default, which at `workers=1` / `1200Mi` gives ~1080MiB — a cap that
  actually fires before the cgroup OOM killer instead of ~2816MiB, which never did.
- `mit_learn`'s explicit `workers_max_rss=1080` needs re-derivation at `workers=1`
  (3200Mi × 0.9 ≈ 2880MiB) or removal in favor of the default. Its inline comment about
  `floor(limit/workers*0.9)` becomes stale either way.
- The `webapp_vpa_max_allowed_memory` docstring caveat ("`--workers-max-rss` … will NOT
  track this ceiling — set `GranianConfig.workers_max_rss` explicitly to keep the two in
  sync") is **inverted**: pinning to the ceiling is now the documented anti-pattern. The
  docstring must say so and point at the follow-on cgroup task.

### 5. Health probes — deferred, not dropped

Splitting liveness to a TCP socket check while readiness stays HTTP (per #663) is the
right end state: an HTTP liveness probe can queue behind a saturated worker and restart a
pod that is merely busy. It is *less* urgent once `backpressure` is 16 instead of 64, and
the nginx sidecar already absorbs part of the risk (the probe hits nginx, not granian).
**Tracked as a separate task**, sequenced after the pilot in stage 2 so probe behavior and
concurrency behavior aren't changed in the same rollout window.

## Capacity math

Per-pod nominal request concurrency:

```
before:  2 workers × 32 blocking_threads = 64
after:   1 worker  ×  8 blocking_threads =  8
```

That 8x drop is the headline risk and the reason for staged rollout. Two things make it
much less severe than the ratio suggests:

1. **The 64 was never real throughput.** All 64 threads contend for two GILs. Useful
   parallelism was bounded by CPU (100m–500m requested, burstable) and by GIL handoff
   overhead, which *rises* with thread count. The pattern in #663 is that reducing this
   number improved p99.
2. **Concurrency is not capacity.** For I/O-bound Django views (DB, redis, external HTTP),
   8 threads still covers well above the requested CPU. Where it binds, the HPA sees
   sustained CPU and adds replicas — which is the intended scaling axis.

What genuinely changes and must be watched: a pod's ability to absorb a *burst* of slow
requests without queuing. That surfaces as increased time-in-queue, not errors, as long as
nginx and the listen backlog hold the overflow.

**Replica re-sizing.** `application_min_replicas` comes from per-stack Pulumi config
(`min_replicas`) for every affected app, so this is a config change per stack, not code.
Baseline recommendation at each stage: leave `min_replicas` alone initially and let the
HPA respond, but pre-raise it for any app whose steady-state CPU utilization already sits
above ~40% of the 60% HPA target, since halving per-pod worker count roughly halves
per-pod CPU and will otherwise trigger a scale-*down* before the load reappears.

## Rollout

Component change lands once; per-app behavior changes as each app's stack is deployed.

> **Correction (stage 0 implementation).** This section originally claimed the component
> change was inert because every affected caller passes `workers=2` explicitly. That only
> held for `workers`. Only `edxapp` pinned `runtime_mode`/`runtime_threads`, and *every*
> WSGI caller would have picked up the new `blocking_threads=8` / `backpressure=16` the
> moment the component landed — applying the headline concurrency change to six apps in
> one deploy, which is what staging exists to avoid. Stage 0 therefore also added
> **holding pins** to every caller not yet in its stage: `runtime_mode="mt"`,
> `runtime_threads=2`, `blocking_threads=32`, `backpressure=64` — the values Granian was
> already deriving from `backlog=128 // workers=2` — plus `runtime_threads=2` alone on
> `mit_learn`/`learn_ai`, whose async interfaces force `blocking_threads=1`.
>
> Each stage below therefore means **"delete that app's holding-pin block"**, not "drop
> the explicit `workers=2`". Verified at stage 0 by reconstructing the pre-change
> `GranianConfig` from git and diffing `build_args()` across all nine call sites: the
> seven WSGI ones differ only by the two new flag pairs, and the two async ones are
> byte-identical.
>
> General form worth carrying forward: "the new default is inert because callers override
> it" must be checked per-field across every caller, and never covers newly-added fields
> that resolve to a non-`None` default.

- **Stage 0 — component.** Land `GranianConfig` fields, defaults, validators, and the
  `workers_max_rss` docstring corrections. No app behavior changes except for any caller
  relying on the `workers` default (none today). Unit tests + `pulumi preview` on one CI
  stack showing zero webapp diffs.
- **Stage 1 — pilot, lower traffic.** `ocw_studio` and `odl_video_service`. Drop the
  explicit `workers=2`, let the new defaults apply. Deploy CI → QA → production. Hold
  ≥ 3 business days at production before proceeding.
- **Stage 2 — mid traffic.** `micromasters`, `xpro`. Same edit. Health-probe split task
  becomes eligible here.
- **Stage 3 — high traffic.** `mitxonline` (plus removal of the ceiling-based
  `workers_max_rss` override), then `edxapp` LMS and CMS separately — CMS first, it takes
  far less traffic than LMS.
- **Stage 4 — async apps.** `mit_learn`, `learn_ai`: `workers=2→1` for `mit_learn` and an
  explicit `backpressure` for both. No `blocking_threads` involvement. Lowest expected
  impact, sequenced last because it shares no evidence with the WSGI stages.

Each stage is one task with its own PR.

## Validation

Before/after per stage, comparing the same weekday-hour window:

- **Latency** — granian request duration p50/p95/p99 from the existing PodMonitor scrape;
  nginx upstream response time as the independent check.
- **Errors** — 5xx rate at nginx and at APISIX; specifically 502/504, which is where
  backpressure saturation would surface.
- **Saturation** — pod CPU utilization vs request, HPA `currentReplicas`, and any
  granian queue/backpressure metric exposed on the metrics port.
- **Memory** — OOMKill count and container restart count; `--workers-max-rss` respawns
  should appear in granian logs *before* any OOMKill, which is the whole point of the
  sizing fix.
- **Startup** — pod ready time, to catch any probe interaction early.

Rollback for any stage is reverting that stage's PR and redeploying; the change is
entirely in container args, so there is no data or schema migration to unwind.

## Open items tracked separately

1. Runtime cgroup-based `--workers-max-rss` (entrypoint wrapper) — evaluate post-rollout.
2. TCP liveness / HTTP readiness probe split — eligible after stage 2.
3. Per-app `blocking_threads` tuning from measured latency, once the uniform 8 is in
   production everywhere.
