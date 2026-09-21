# Grafana alerting: 30-day re-measurement after W0-W5

Date: 2026-09-21
Evidence window: 2026-08-22 → 2026-09-21
Baseline: [grafana-alerting-holistic-analysis.md](grafana-alerting-holistic-analysis.md),
window 2026-07-08 → 2026-08-07
Sequence and workstream numbering: [grafana-alerting-remediation-spec.md](grafana-alerting-remediation-spec.md)

The remediation spec gates the ML pilot (W7) on "W0-W4 landing and a 30-day clean
measurement", and W6 asks for the analysis's measurement queries to be re-run 30 days
after W3. W3 landed 2026-08-24, so that window has now closed. This is that measurement.

All counts are measured. Sources: the production Grafana alert state history
(`grafanacloud-alert-state-history`), the Rootly alert API, the production and QA
`grafana-ml-app` job APIs, and the production ML metrics tenant.

---

## 1. Headline: Rootly alert volume is down 57%

| | Baseline (07-08 → 08-07) | Now (08-22 → 09-21) |
|---|---:|---:|
| Rootly alerts, 30d | 1,449 | 625 |
| Per day | ~48 | ~21 |

The drop is concentrated at W3's landing rather than spread across the window. Counting
by alert age:

| Period | Alerts | Rate |
|---|---:|---:|
| 08-09 → 08-17 | 400 | ~50/day |
| 08-18 → 08-24 | 60 | ~10/day |
| 08-24 → 09-21 | 599 | ~21/day |

The rate falls between the 08-17 and 08-18 probes. W3 merged as
[#5503](https://github.com/mitodl/ol-infrastructure/pull/5503) on 2026-08-18, so the
inflection lands on the deploy to within a day.

### Counting method, because the obvious one is wrong

`list_alerts` ignores the `created_at_gt` / `created_at_lt` filters: a 1-day window and a
30-day window both return `total_count: 4836`, which is the all-time total. Anything that
trusts those filters will report 4,836 as a 30-day figure. It is not.

Alerts are returned newest-first, so the count in a window is the difference between the
page indices bracketing it at `page_size=1`. Binary-searching for the boundary dates:

| Page | `created_at` |
|---:|---|
| 1 | 2026-09-21 |
| 625 | 2026-08-22 |
| 1180 | 2026-08-07 |
| 2629 | 2026-07-10 |

Page 2629 − page 1180 = **1,449**, which reproduces the analysis's published baseline
exactly, over the same window. The method is sound, so the 625 above can be trusted on
the same footing as the number it is being compared against.

---

## 2. Firings per rule, production stack

Baseline figures are the analysis's §2.2 table. Rules absent from one column did not fire
in that window.

| Rule | Baseline | Now | |
|---|---:|---:|---|
| `PodOOMKilledCritical` | 195 | 43 | −78% |
| `HPAAtMaxReplicasCritical` | 278 | 254 | −9% |
| `PodCrashLoopingCritical` | 52 | **849** | **+1,533%** |
| `DeploymentUnavailableCritical` | 26 | 0 | gone |
| `StatefulSetReplicasMissingCritical` | 22 | 7 | −68% |
| `KubernetesJobFailedCritical` → `WorkloadJobFailedCritical` | 11 | 102 | renamed in W3 |
| `HTTPRequestDurationTooHighAvg [5m]` | 1,168 | 480 | still plugin-owned, still dropped |
| `ProbeFailedExecutionsTooHigh [5m]` | 55 | 7 | still dropped |

New since the baseline:

| Rule | Now | Note |
|---|---:|---|
| `APISIXEdge5xxRateFast` | 476 | W4 coverage, working as designed |
| `DagsterPgBouncerConnectionChurnCritical` | 458 | |
| `DiskUsageCritical` | 148 | see §4 |
| `APISIXOIDCCallbackFailureRateChronic` | 38 | |
| `APISIXEdge5xxRateSlow` | 31 | |
| `DagsterDaemonHeartbeatStaleCritical` | 20 | |

Three of the original four noise rules came down, and `DeploymentUnavailableCritical`
went silent entirely. The fourth, `PodCrashLoopingCritical`, went up, but §3 shows that
is a broken workload rather than a rule that was missed.

Firing counts are alert-instance state transitions, not Rootly
deliveries: 849 `PodCrashLooping` transitions against 625 total Rootly alerts from all
sources and all three stacks, so grouping is absorbing most of them. The two are
different units and should not be subtracted from each other.

---

## 3. `PodCrashLoopingCritical`: the rule is fine, a workload broke

The 52 → 849 jump reads like a regression in the rule. It is not. 830 of the 849 firings
are two workloads:

| Namespace | Pod | Firings |
|---|---|---:|
| `superset` | `superset-mcp-6c89c564c8-7b4tj` | 440 |
| `superset` | `superset-mcp-6886bccf8d-c4ssx` | 165 |
| `superset` | `superset-mcp-6c89c564c8-tml5l` | 101 |
| `superset` | `superset-mcp-6c89c564c8-fnkkd` | 4 |
| `mitxonline-openedx` | `mitxonline-ts-sts-1` | 116 |
| `mitxonline-openedx` | `mitxonline-ts-sts-0` | 4 |

Everything else on the stack accounts for the remaining 19, down from 52 at the baseline.
`superset-mcp` alone is 710 across four pod names and two ReplicaSets, and it is still
crashlooping: 231 firings in the three days to 2026-09-21.

It cannot have been a contributor at the baseline, because the entire rule fired 52 times
in that window. So this is a workload that broke during the measurement period and has
stayed broken for at least 30 days without anyone acting on it, which is a `superset-mcp`
problem rather than an alerting one.

W3 did treat this rule. [#5503](https://github.com/mitodl/ol-infrastructure/pull/5503)
gave `PodOOMKilled*` and `PodCrashLooping*` the same `keep_firing_for="30m"` and
`missing_series_evals_to_resolve=10`, and added one notification-policy branch matching
`alertname=~"Pod(OOMKilled|CrashLooping)(Warning|Critical)"` that groups both at
namespace level with `group_interval=30m` / `repeat_interval=12h`. Both halves of the fix
cover both rules.

The grouping half is demonstrably working: 849 firings on this rule against 625 Rootly
alerts from every source and all three stacks, so the great majority of these transitions
never became a page.

### The open question

`keep_firing_for="30m"` is meant to hold an alert open across a churning pod's
disappearance. A single pod name (`…-7b4tj`) recorded 440 firings over 30 days, roughly
15 a day, which is not what a 30-minute hold on a continuously failing pod should look
like. Either the condition is flapping faster than the hold covers, or the hold is not
doing what it was expected to do.

That is worth checking, but it is a question about `keep_firing_for`, not evidence that
the rule was left untreated. It should be answered against a workload that is not also
genuinely broken.

---

## 4. `DiskUsageCritical`: one full disk, 148 alert instances

All 148 firings landed in the three days to 2026-09-21, all on one host
(`ip-10-0-3-130`, a Concourse worker), one per mountpoint of the form
`/var/concourse/worker/volumes/live/<uuid>/volume`.

The rule in `metric_rules/linux_host.py:106` filters on `device`, not on mountpoint:

```promql
(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 95
```

Concourse mints a live volume per build step, each a separate mountpoint on the same
underlying device, so one worker filling up produces one alert per volume present at the
time. The underlying disk pressure is real and already tracked separately (PR #5933
resizes the infra workers 300GB → 1000GB); the alerting shape is the defect here.

Unlike §3, this one is a rule defect rather than a broken workload. `linux_host.py` was
explicitly out of scope for W3, which was "scoped to `metric_rules/eks_general.py` only"
per #5503's own commit message, so this rule has had none of the storm treatment applied
to it.

---

## 5. Grafana Cloud ML: the surface grew 6.5x while nobody was looking

| | Baseline | Now |
|---|---:|---:|
| Forecast jobs, production | 5 | **34** |
| Forecast jobs, QA | 5 | **31** |
| Forecast jobs, CI | — | 0 |
| Outlier detectors, anywhere | 0 | **0** |
| Hand-created jobs of any kind | 0 | **0** |
| Production firings, 30d | 2,812 | **10,138** |

Every one of the 65 jobs was auto-created by the Adaptive Traces plugin, and every one
carries identical untuned hyperparameters: `growth: flat`, `daily_seasonality: 10`,
`weekly_seasonality: 15`, a 21-day training window, a 240s interval. Nobody has ever
created an ML job here deliberately.

The top firers on production, 30d: `mitx_production_edxapp_lms` 2,573,
`learn_nextjs` 2,563, `learn_webapp` 1,534, `apisix` 1,257,
`mitxonline_production_edxapp_cms` 739.

`learn_webapp` was the analysis's example at 944 firings. It is now 1,534, and it is no
longer even the worst. The plugin added 29 production jobs in six weeks without anyone
asking it to, which is the strongest available argument for not letting the vendor decide
what the ML surface is.

None of this reaches a human, and the volume confirms it independently: 10,138 ML firings
against 625 total Rootly alerts from every source. Routed as-is, ML would not triple page
volume as the analysis estimated, it would multiply it by roughly sixteen.

### Billable footprint, for the quote

The production ML tenant carries 102 metric names (34 jobs × `:actual`, `:anomalous`,
`:predicted`). Series per job vary by span cardinality rather than by anything we control.
Sampled `:predicted` series counts:

| Job | Series |
|---|---:|
| `mitx_production_edxapp_lms` | 255 |
| `learn_webapp` | 132 |
| `apisix` | 84 |
| `production_dagster_code_lakehouse` | 15 |
| `production_toolhive_swe_sentry` | 6 |

`:predicted` carries three `ml_forecast` label values, `:actual` and `:anomalous` one
each, so a job's total is roughly 1.67× its `:predicted` count. Against a sample mean of
~98 that puts the estate at order 10⁴ series across the 65 jobs. This is an estimate from
five sampled jobs, not a census: the ML tenant rejects regex matchers, so a real total
needs 102 individual queries. For scale, the main metrics tenant bills 945,441 series.

---

## 6. What this means for W7

**The gate is met.** W0-W5 landed, volume is down 57%, the duplicate pipeline is gone,
and the severity split is in place. There is a clean 30-day before/after.

**The pilot as specified is now the wrong next action.** Both the analysis (§6.6) and the
spec (W7) scope it as "add 2-3 forecast jobs, Slack only, 30 days". That was written when
5 jobs existed. 65 exist. Adding three more to a surface nobody is managing does not
answer the question the pilot was meant to answer, and it makes the surface worse.

The first ML decision is what to do about the 65 that are already running and already
billed. Three options, and they should be decided before any job is added:

1. Leave them. They cost money and produce nothing a human sees, but they feed Adaptive
   Traces' sampling control, which is what they exist for.
2. Delete the QA set (31 jobs). QA's forecasts inform QA's trace sampling and little
   else, and §0.9 of the spec already established QA gives almost no calibration signal.
3. Keep a chosen few, tuned, and delete the rest.

Whichever way that goes, the pilot's own scope stands with one change: it should be three
jobs we define, tuned with the severity grading, minimum deviation magnitude and real
confirmation window the auto-created ones lack, and it should be measured against the
untuned Adaptive Traces jobs as the control group rather than against nothing.

The hard caveat is unchanged and now has a second example. An anomaly model trained on
`courses-backend` at 33% 5xx learns 33% as normal. A model trained on `superset-mcp`
crashlooping for 30 continuous days learns crashlooping as normal. ML answers "did this
change?", never "is this acceptable?" It supplements W4's absolute-level SLO alerting; it
never replaces it.

**Cost remains the blocker.** Still unquoted, and still the one thing that cannot be
settled from inside the stack. §5 gives the footprint to quote against.

---

## 7. Follow-up work this surfaced

- `superset-mcp` has been crashlooping in production for at least 30 days and is unowned.
  This is the one with a live production impact.
- `keep_firing_for="30m"` does not appear to be holding alerts across a churning pod the
  way #5503 intended. See §3; check it against a workload that is not also broken.
- `DiskUsageCritical` needs a mountpoint predicate so one full Concourse worker is one
  alert. `linux_host.py` was out of scope for W3 and has had none of the storm treatment.
- The Rootly `noise` field is still unused. Every alert sampled in this window reads
  `noise: null` or `not_noise`, so W6 has not started and the next tuning round will have
  no data behind it again.

## Appendix: queries used

```
# Rootly window count: binary-search the page index, do not trust the date filters
GET /v1/alerts?page[number]=<n>&page[size]=1     # newest-first; count = page(start) - page(end)

# Firings per rule, 30d, instant query (a range query over [30d] exceeds the Loki limit)
GET /api/datasources/proxy/uid/grafanacloud-alert-state-history/loki/api/v1/query
  query=topk(40, sum by (ruleTitle) (count_over_time({from="state-history"} | json | current =~ `Alerting.*` [30d])))

# Firings per resource
  query=topk(25, sum by (ruleTitle, labels_namespace, labels_pod) (count_over_time({from="state-history"} | json | current =~ `Alerting.*` [30d])))

# ML job inventory, per stack
GET /api/plugins/grafana-ml-app/resources/manage/api/v1/jobs
GET /api/plugins/grafana-ml-app/resources/manage/api/v1/outliers

# ML series footprint (regex matchers are rejected on this tenant; name the metric)
GET /api/datasources/proxy/uid/grafanacloud-ml-metrics/api/v1/label/__name__/values
GET /api/datasources/proxy/uid/grafanacloud-ml-metrics/api/v1/query?query=count(<metric>)
```
