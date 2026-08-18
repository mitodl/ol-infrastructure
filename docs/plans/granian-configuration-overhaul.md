# Granian configuration overhaul

**Status:** stage 0 merged 2026-07-23 (#5083); stage 1 merged 2026-07-27 (#5135), validated
in production 2026-08-07; stage 2 merged 2026-08-10 (#5344), validated in production
2026-08-17; stage 3 `mitxonline` **blocked** (see stage 3), `edxapp` unblocked; stage 4
pending
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

- ~~`mitxonline`'s ceiling-derived override (`__main__.py:536-543, 563-569`) is
  **removed**, along with `MITXONLINE_GRANIAN_WORKERS` and `GRANIAN_MASTER_OVERHEAD_MIB`.
  It reverts to the component default, which at `workers=1` / `1200Mi` gives ~1080MiB — a
  cap that actually fires before the cgroup OOM killer instead of ~2816MiB, which never
  did.~~

  > **Retracted 2026-08-17, before stage 3 was written.** This is wrong and would have
  > broken production. `mitxonline`'s VPA runs an admission controller: pods are
  > *admitted* at request=limit=3Gi, carrying
  > `vpaUpdates: Pod resources updated by mitxonline-app-memory-vpa`. The `1200Mi` in the
  > Deployment template is never the enforced limit — over 14 days
  > `kube_pod_container_resource_limits` for the container ranged 1953MiB–3072MiB and
  > never touched 1200MiB. Measured usage is avg 992MiB working set, **p95 2645MiB**,
  > peak 2978MiB. The component default would derive 1080MiB and put the single worker
  > into a permanent respawn loop at normal traffic.
  >
  > The "derive from the current declared limit, not the ceiling" rule assumes the pod
  > starts at its declared floor and the VPA grows it later, leaving a window where a
  > ceiling-derived cap is above the real cgroup limit. With an admission-time mutation
  > there is no such window: ceiling *is* the current declared limit, from the pod's first
  > instant. For `mitxonline` the two answers converge.
  >
  > **Corrected action:** keep the ceiling-derived override and retarget it at one worker
  > — `MITXONLINE_GRANIAN_WORKERS` 2 → 1, giving `(3072 − 256) // 1 = 2816MiB`, still
  > under the 3072MiB limit. Keep `GRANIAN_MASTER_OVERHEAD_MIB` and
  > `mitxonline_granian_workers_max_rss`. The rest of the stage-3 `mitxonline` edit
  > (dropping the concurrency holding pins) is unaffected. Tracked as
  > `tk-stage-3-blocker-mitxonline-s-vpa-never-runs-at-t-cc6acf`; lesson
  > `les-a-vpa-with-an-admission-controller-makes-resourc-944b12`.
  >
  > This makes `mitxonline` the strongest case for the runtime cgroup-derived cap
  > (open item 1): it is the only app where the synth-time limit and the runtime limit
  > differ by 2.5x.
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

Baseline recommendation at each stage: **leave `min_replicas` alone.** Do not pre-raise.

> This reverses the rule originally stated here, which was to pre-raise for any app whose
> steady-state CPU utilization already sat above ~40% of the 60% HPA target, "since
> halving per-pod worker count roughly halves per-pod CPU". **That premise is false and
> was disproven by measurement in stage 2.** The same traffic arriving at the same number
> of pods performs the same work regardless of how many worker processes divide it —
> worker count changes *capacity*, not *usage*. Measured p95 CPU across the stage-2
> deploys: `micromasters` 0.0100 → 0.0097, `xpro` 0.0631 → 0.0608. Flat, not halved.
> There is no post-deploy scale-*down* to guard against, so there is nothing to pre-raise
> against. See the stage-3 `mitx` LMS entry below, which is where this was caught.

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
- **Stage 1 — pilot, lower traffic.** `ocw_studio` and `odl_video_service`. Delete each
  app's holding-pin block, letting the new defaults apply. Deploy CI → QA → production.
  Hold ≥ 3 business days at production before proceeding.

  Replica pre-raise was evaluated against production and **not** applied. Over the 7 days
  to 2026-07-27 both webapps sat pinned at `min_replicas=2` with p95 CPU ≈ 3m/pod
  (≈ 3% of `ocw_studio`'s 100m request, ≈ 1% of `odl_video_service`'s 250m) and 7-day
  peaks of 26m and 9m. The HPA's 60% target has never been approached in either
  direction, so there is no scale-down to guard against. Peak working set over the same
  window — 904MiB of a 3Gi limit (`ocw_studio`) and 423MiB of 1Gi
  (`odl_video_service`) — stays under the new single-worker RSS caps of 2764MiB and
  921MiB, so the cap still fires ahead of the cgroup OOM killer.
- **Stage 2 — mid traffic.** `micromasters`, `xpro`. Same edit. Health-probe split task
  becomes eligible here.

  Replica pre-raise was evaluated against production and **not** applied. Over the 7 days
  to 2026-08-10 both webapps sat pinned at `min_replicas=2` — with no scale-down possible
  from an already-minimum replica count, the scale-down risk the pre-raise guards against
  doesn't apply regardless of CPU headroom. p95 CPU ≈ 10m/pod for `micromasters` (≈ 4% of
  its 250m request) and ≈ 62m/pod for `xpro` (≈ 25% of its 250m request); zero container
  restarts for either app over the same window. Peak working set — ≈ 928MiB of a 2000Mi
  limit (`micromasters`) and ≈ 1236MiB of a 2Gi limit (`xpro`) — stays under the new
  single-worker RSS caps of 1800MiB and 1843MiB, so the cap still fires ahead of the
  cgroup OOM killer.

  **Validated 2026-08-17.** Production deploys landed 2026-08-10 18:37Z (`xpro`) and
  2026-08-12 18:07Z (`micromasters`); both args verified live on the Deployments. Windows
  compared are weekday-aligned 4-day spans (before ending 2026-08-10 18:00Z, after ending
  2026-08-17 14:00Z) with near-identical nginx log volume (1.21M lines each).

  | signal | `micromasters` before → after | `xpro` before → after |
  | --- | --- | --- |
  | throughput (req/s) | 0.332 → 0.324 | 1.059 → 1.139 |
  | `granian_blocking_queue` avg / max | 0.0000085 / 1 → 0 / 0 | 0.0013 / 7 → 0.0119 / 8 |
  | blocking-thread busy µs per request | 31 431 → 27 771 (−12%) | 142 122 → 135 327 (−5%) |
  | `granian_py_wait` µs per request | 22.5 → 49.1 | 681 → 989 |
  | `granian_connections_err` | 0 → 0 | 0 → 0 |
  | RSS / error worker respawns | 0 / 0 → 0 / 0 | 0 / 0 → 0 / 0 |
  | p95 CPU, all pods (cores) | 0.0100 → 0.0097 | 0.0631 → 0.0608 |
  | peak working set | 912MiB → 524MiB (cap 1800) | 1171MiB → 607MiB (cap 1843) |
  | container restarts | 0 → 0 | 0 → 0 |
  | webapp HPA `currentReplicas` | 2 → 2 | 2 → 2 |
  | nginx 5xx | 34 → 3 | 1 → 0 |
  | nginx 502/504 | 0 → 1 | 1 → 0 |

  No regression on any axis. The two directional moves are both non-events in absolute
  terms: `xpro`'s mean blocking-queue depth rose 9× to 0.012 requests (max depth is
  unchanged at 7→8, and it was *already* queueing at 32 threads, which is the GIL-
  contention pattern the change targets), and per-request GIL wait rose to 989µs against
  135ms of thread-busy time — 0.7% of service time. Memory is the headline win: peak
  working set fell 43–48%, so both apps now sit at ~30% of their RSS cap instead of ~50%
  of a cap sized for two workers. The one post-change 502 was a single readiness probe
  during a pod start on 2026-08-13; the other two 5xx were application-level Django 500s
  (a social-auth callback and an enrollment POST), unrelated to Granian.

  **Instrumentation gap found while validating.** The spec's "latency p50/p95/p99" is not
  measurable for these apps: Granian's metrics endpoint exposes only counters and gauges
  (no request-duration histogram), the nginx sidecar uses the stock combined log format
  with no `$request_time`/`$upstream_response_time`, and neither app is fronted by APISIX
  or Traefik. Busy-µs-per-request plus queue depth were used as the proxy. Stage 3
  (`mitxonline`, `edxapp`) *is* behind APISIX and has `apisix_http_latency_bucket`, so it
  gets real percentiles; stage 4's `mit_learn` needs this checked before its window opens.
- **Stage 3 — high traffic.** `mitxonline` (plus ~~removal of~~ **retargeting** the
  ceiling-based `workers_max_rss` override — see the retraction under §4), then `edxapp`
  LMS and CMS separately — CMS first, it takes far less traffic than LMS.

  **`mitxonline` is blocked as of 2026-08-17** on two findings, both raised while opening
  the stage:

  1. `tk-stage-3-blocker-mitxonline-s-vpa-never-runs-at-t-cc6acf` — the
     `workers_max_rss` retraction above. The corrected edit is known; it needs sign-off
     because it inverts what this plan told the implementer to do.
  2. `tk-mitxonline-is-the-only-granian-workload-in-produ-b3dffd` — **root-caused and
     fixed the same day; awaiting deploy.** `mitxonline` was the only Granian workload in
     production emitting no `granian_*` series, for the entire 158 days its PodMonitor
     had existed.

     Prometheus SD flattens a label name by replacing every non-alphanumeric character
     with `_`, so `ol.mit.edu/pod-security-group` and `ol.mit.edu/pod_security_group`
     both become `__meta_kubernetes_pod_label_ol_mit_edu_pod_security_group`. The
     PodMonitor selector was the app's full label set, which for `mitxonline` contains
     both — the component always sets the hyphenated one, and
     `K8sAppLabels.pod_security_group` emits the underscored one for its single caller,
     `mitxonline`. That generated two `keep` rules on one meta-label demanding
     `(mitxonline-app-access-production-e9f43bb)` and `(mitxonline)`. Unsatisfiable, so
     zero targets — and therefore no `up` series to alert on, no scrape error, and a
     PodMonitor that looks perfectly healthy. Alloy's own
     `net_conntrack_dialer_conn_attempted_total` for the pool reads `0` against 512 for
     `micromasters`, which is the tell.

     Fixed by narrowing the PodMonitor selector to namespace + application +
     `component=webapp` (celery pods carry `component=celery`), with a regression test —
     the failure mode is silent, so nothing else would catch it. This also stops a
     security group replacement silently breaking any app's scrape, which was a latent
     hazard everywhere, not just here. Lesson
     `les-two-k8s-labels-differing-only-in-vs-collide-unde-bb7ec4`; the redundant
     `K8sAppLabels.pod_security_group` field still wants deleting, but it sits in the
     Deployment's immutable `spec.selector`, so that needs a replacement window
     (`tk-delete-the-redundant-k8sapplabels-pod-security-g-ac508d`).

     **`mitxonline` stage 3 stays blocked until this is deployed and verified**, because
     `granian_workers_respawns_for_rss` is exactly the signal that would show a
     retargeted RSS cap thrashing. Confirm with
     `count by (namespace) (granian_workers_spawns)` and `up{namespace="mitxonline"}`
     after the production deploy.

  **Resequenced 2026-08-17: `edxapp` CMS goes first, and LMS is pulled out of stage 3.**
  With `mitxonline` blocked and `edxapp` blocked by nothing, CMS went ahead. Measuring to
  order the rest produced a result the plan did not anticipate.

  Concurrency demand per pod, measured as
  `sum by (pod) (rate(granian_blocking_busy_cumulative[5m])) / 1e6` — mean
  concurrently-busy blocking threads. Today each pod has 2 × 32 = 64; after the change,
  1 × 8 = 8.

  | workload | pods | rps | mean | **p99** | max |
  | --- | --- | --- | --- | --- | --- |
  | `mitxonline` CMS | 3 | 0.9 | 0.06 | **8.7** | 21.6 |
  | `mitxonline` **LMS** | 16 | 62.4 | 0.41 | **17.7** | 56.4 |
  | `mitx` LMS | 4 | 4.4 | 0.11 | 0.27 | 20.9 |
  | `mitx` CMS | 2 | 0.25 | 0.005 | 0.07 | 8.0 |
  | `mitx-staging` CMS/LMS | 1 each | ~0.15 | ~0.06 | ~0.07 | ~1 |

  Two workloads exceed the new default of 8 at p99, and the gap between them is the
  whole decision. `mitxonline` CMS is *marginally* over — 8.7 against 8, within the
  noise of a p99 computed over 3 pods at 0.9 rps — which is a capacity to watch, not a
  capacity to reject; see the CMS risk assessment below. `mitxonline` LMS is over by
  more than **2×** — 17.7 against 8 — which is a different claim entirely. Everything
  else in the estate is one to two orders of magnitude below 8.

  So LMS does not want the default at all — it wants `blocking_threads` set explicitly
  from measurement, somewhere around 16–24. That is this plan's own open item 3 arriving
  early, and it is tracked as
  `tk-edxapp-lms-needs-blocking-threads-sized-from-mea-317fe2`.

  Note what this says about the original ordering. "CMS first, it takes far less traffic
  than LMS" is true on traffic and wrong on risk: `mitxonline` CMS at 0.9 rps needs p99
  8.7 threads while `mitx` LMS at 4.4 rps needs 0.27. Concurrency is rate × service time,
  and Studio's long authoring operations dominate the second term. **Ordering a staged
  rollout by request rate put the riskier workload in the "safe canary" slot.** Method
  recorded as `pat-size-granian-blocking-threads-from-busy-thread-c-3a8b48`; note in
  particular that `granian_blocking_threads` measures threads *spawned*, not busy, and
  reads 32 for every edxapp workload including those needing 0.07.

  CMS was judged safe despite p99 8.7 sitting right at the ceiling: 0.9 rps over 3 pods,
  and the spikes are Studio import/export bursts whose blast radius is a slow authoring
  operation rather than learner traffic. Its `workers_max_rss` is unchanged in aggregate
  for every install — the component derives `floor(limit / workers × 0.9)`, so halving
  the worker count doubles the per-worker cap and the pod total is identical whatever the
  declared limit (`mitxonline` at 4Gi: 2 × 1843MiB → 1 × 3686MiB; `mitx` and
  `mitx-staging` at 2Gi: 2 × 921MiB → 1 × 1843MiB; both verified in the CI preview diff).
  What *does* change is that a respawn now costs the pod's whole serving capacity instead
  of half; CMS showed 71 RSS respawns and 77 restarts over 14 days but **zero over the
  last 3**, once its VPA grew the pods past the declared 4Gi. LMS respawns, by contrast,
  are ongoing (8 and 10 over 14 days), which is a second independent reason to keep it at
  2 workers for now.

  > Growing *past* the declared 4Gi limit looks impossible given
  > `webapp_vpa_max_allowed_memory="4Gi"`, and a reviewer read it that way. It is not.
  > `maxAllowed` bounds the **request**, and `controlledValues: RequestsAndLimits` then
  > scales the limit to preserve the pod spec's original request:limit ratio — 2Gi:4Gi,
  > i.e. 2×. So the effective limit ceiling is 8Gi, not 4Gi. Confirmed live in
  > `applications-production`: request 2990.9MiB (under the 4096MiB `maxAllowed`), limit
  > 5981.8MiB, ratio exactly 2.0, and the 14d limit series ranges 4096–7755MiB. Anything
  > sizing a cap against `maxAllowed` needs to multiply by that ratio first.

  **LMS canary — `mitx` first, via new per-install config.** Because `k8s_resources.py`
  is shared, there was no way to move one install's LMS without moving all three. Added
  `edxapp:k8s_granian.lms` per-stack config: omitted (the default for every stack) keeps
  the pre-overhaul holding pins, and an install opts in by setting it. `mitx` CI/QA/
  Production now set `workers: 1, runtime_threads: 1, blocking_threads: 8,
  backpressure: 16`. Verified in preview: `mitx.CI` updates both edxapp Deployments (LMS
  to the new args, CMS from the change above) while `mitxonline.CI` updates **only** CMS —
  its LMS does not appear in the diff at all, so the refactor is a no-op for anything that
  has not opted in.

  Be honest about what this canary does and does not prove. `mitx` LMS p99 concurrency is
  0.27 busy threads against the new ceiling of 8 — 30× headroom — so it will never
  approach the limit. It validates that LMS boots and serves on one worker, that
  `runtime_mode` auto is fine on this code path, the whole-pod respawn blast radius, and
  probe/startup behavior. It does **not** test whether 8 threads is enough at LMS-scale
  concurrency, because `mitx` LMS has no such concurrency. Only `mitxonline` LMS does, and
  that is precisely the risk being deferred.

  **Replica pre-raise: not applied. This is where the plan's original rule was caught.**
  Stage 3 is the first stage where it mattered — stages 1 and 2 dodged it because every
  app was pinned at `min_replicas`, whereas `mitx` LMS genuinely scales (observed 4→15
  over 14 days). Under the rule as originally written ("pre-raise for any app whose
  steady-state CPU sits above ~40% of the 60% HPA target", i.e. above 24% utilization),
  `mitx` LMS at p95 89m against a 250m request — 36% — would have qualified.

  It does not, because the premise is false. "Halving per-pod worker count roughly halves
  per-pod CPU" does not hold: the same traffic arriving at the same number of pods
  performs the same work regardless of how many worker processes divide it. Worker count
  changes *capacity*, not *usage*. Stage 2 measured exactly this and it is in the table
  above — `micromasters` p95 CPU 0.0100 → 0.0097 and `xpro` 0.0631 → 0.0608, flat rather
  than halved. There is no post-deploy scale-down to guard against, so no pre-raise. The
  **Replica re-sizing** baseline earlier in this document has been rewritten to say so
  directly, rather than leaving the disproven rule standing to be contradicted here.

  **Blast radius.** `k8s_resources.py` is shared, so the CMS edit changes CMS for
  `mitxonline-openedx`, `mitx-openedx` and `mitx-staging-openedx` as each stack deploys —
  three deployments, not one. `xpro-openedx` is *not* affected: it still runs the
  hand-rolled pre-`OLApplicationK8s` deployments (175 days old, granian args with no
  `--blocking-threads`, `--backpressure` or `--metrics`, i.e. predating stage 0). Whenever
  that stack next deploys it jumps roughly six months of drift in one step, which is worth
  handling deliberately rather than discovering mid-incident
  (`tk-xpro-edxapp-is-6-months-stale-still-on-the-hand--43a5a4`).

  `edxapp` is **not** affected by either finding — `mitxonline-openedx` reports
  `granian_*` normally (`cms-edxapp-webapp-pod-monitor` and `lms-edxapp-webapp-pod-monitor`
  both up), and it is behind APISIX so `apisix_http_latency_bucket` gives it the real
  latency percentiles that stage 2 had to proxy for. Doing `edxapp` CMS first while
  `mitxonline` is unblocked is a viable resequencing, but it is a production-rollout
  ordering change and belongs to whoever owns the rollout, not to the implementer.
- **Stage 4 — async apps.** `mit_learn`, `learn_ai`: `workers=2→1` for `mit_learn` and an
  explicit `backpressure` for both. No `blocking_threads` involvement. Lowest expected
  impact, sequenced last because it shares no evidence with the WSGI stages.

Each stage is one task with its own PR.

## Validation

Before/after per stage, comparing the same weekday-hour window:

- **Latency** — granian request duration p50/p95/p99 from the existing PodMonitor scrape;
  nginx upstream response time as the independent check.

  > **Correction (stage 2 validation).** Neither source exists. Granian's metrics endpoint
  > exposes only counters and gauges — there is no request-duration histogram — and the
  > nginx sidecar logs the stock combined format with no `$request_time` /
  > `$upstream_response_time`. Real percentiles are available *only* for apps behind
  > APISIX (`apisix_http_latency_bucket`): `mitxonline` and `edxapp`. For `micromasters`,
  > `xpro`, `ocw_studio` and `odl_video_service` the working proxy is
  > `rate(granian_blocking_busy_cumulative) / rate(granian_requests_handled)`
  > (µs of thread time per request) plus `granian_blocking_queue` avg/max for saturation.
  > Check `mit_learn` before stage 4 opens. Lesson
  > `les-granian-request-latency-percentiles-are-unmeasur-d2f2ab`.
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
