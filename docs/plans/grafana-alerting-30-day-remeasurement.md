# Grafana alerting: 30-day re-measurement after W0-W5

Date: 2026-09-21
Evidence window: 2026-08-22 → 2026-09-21
Baseline: [grafana-alerting-holistic-analysis.md](grafana-alerting-holistic-analysis.md),
window 2026-07-08 → 2026-08-07
Sequence and workstream numbering: [grafana-alerting-remediation-spec.md](grafana-alerting-remediation-spec.md)

The remediation spec gates the ML pilot (W7) on "W0-W4 landing and a 30-day clean
measurement", and W6 asks for the analysis's measurement queries to be re-run 30 days
after W3. W3 merged as [#5503](https://github.com/mitodl/ol-infrastructure/pull/5503) on
2026-08-18, so that window has now closed. This is that measurement.

Counts are measured unless stated otherwise; the ML series footprint in §5 is the one
figure that is a sample-based estimate, and it is labelled there. Sources: the production
Grafana alert state history
(`grafanacloud-alert-state-history`), the Rootly alert API, the production and QA
`grafana-ml-app` job APIs, and the production ML metrics tenant.

---

## 1. Headline: Rootly alert volume is down 57%

| | Baseline (07-08 → 08-07) | Now (08-22 → 09-21) |
|---|---:|---:|
| Rootly alerts, 30d | 1,459 | 624 |
| Per day | ~49 | ~21 |

Both figures are page-index differences measured the same way (see below). The analysis
published 1,449 for the baseline window by other means; this method gives 1,459 over the
same dates, a 0.7% disagreement.

The drop is concentrated at W3's landing rather than spread across the window. Counting
by alert age:

| Period | Alerts | Rate |
|---|---:|---:|
| 08-09 → 08-17 | 400 | ~50/day |
| 08-18 → 08-24 | 60 | ~10/day |
| 08-24 → 09-21 | 606 | ~22/day |

The rate falls between the 08-17 and 08-18 probes. W3 merged as
[#5503](https://github.com/mitodl/ol-infrastructure/pull/5503) on 2026-08-18, so the
inflection lands on the deploy to within a day.

### Counting method, because the obvious one is wrong

`list_alerts` ignores the `created_at_gt` / `created_at_lt` filters: a 1-day window and a
30-day window both return the same `total_count`, which is the all-time total. Anything
that trusts those filters will report that figure as a 30-day count. It is not one.

Alerts are returned newest-first, so the count in a window is the difference between the
page indices bracketing it at `page_size=1`. Binary-searching for the boundary dates:

| Page | `created_at` | |
|---:|---|---|
| 1 | 2026-09-21 16:08 | newest at time of measurement |
| 607 | 2026-08-24 | |
| 632 | 2026-08-22 09:06 | start of the current window |
| 667 | 2026-08-18 | |
| 707 | 2026-08-17 | |
| 1107 | 2026-08-09 | |
| 1187 | 2026-08-07 16:37 | end of the baseline window |
| 2646 | 2026-07-08 08:31 | start of the baseline window |
| 2647 | 2026-07-07 23:27 | last alert before that window |

Current window: 632 − 1 = **624**. Baseline window: 2646 − 1187 = **1,459**, against the
1,449 the analysis published for the same dates.

Two things make this method easy to get wrong, and both bit this measurement:

**The index drifts as new alerts arrive.** Page numbers are relative to the newest alert,
so a probe taken an hour later addresses a different alert. `total_count` went from 4,836
to 4,843 during this session, and every page index shifted by exactly 7. The table above
is normalized to the later pass; the same alert sat at page 1,180 in the first pass and
1,187 in the second. Probes have to be taken in one pass, or offset-corrected against a
known fixed alert.

**A boundary probe that lands near the window edge is not the same as one on it.** The
first pass of this measurement used a page dated 2026-07-10 as the baseline start and got
exactly 1,449, matching the published figure. That was a coincidence: the true 07-08
boundary is page 2646, and the window it bounds holds 1,459. The ten-alert gap is the
alerts between 07-08 and 07-10. Reproducing a number is not the same as reproducing it
over the same window, and only the second one validates anything.

So the method cross-checks against the published baseline to within 0.7% rather than
exactly. That is still good agreement between two independent counts, and nothing in this
document turns on the difference, but it is a cross-check and not a reproduction.

---

## 2. Firings per rule, production stack

Baseline figures are the analysis's §2.2 table. Rules absent from one column did not fire
in that window.

| Rule | Baseline | Now | |
|---|---:|---:|---|
| `PodOOMKilledCritical` | 195 | 43 | −78% |
| `HPAAtMaxReplicasCritical` | 278 | 254 | −9% |
| `PodCrashLoopingCritical` | 52 | 849 | _+1,533%_ |
| `DeploymentUnavailableCritical` | 26 | 0 | gone |
| `StatefulSetReplicasMissingCritical` | 22 | 7 | −68% |
| `HTTPRequestDurationTooHighAvg [5m]` | 1,168 | 480 | still plugin-owned, still dropped |
| `ProbeFailedExecutionsTooHigh [5m]` | 55 | 7 | still dropped |

`KubernetesJobFailedCritical` (11 at the baseline) is deliberately left out of that table.
It became `WorkloadJobFailedCritical`, now at 102, but the rename was
[#5457](https://github.com/mitodl/ol-infrastructure/pull/5457) rather than W3, and
[#5496](https://github.com/mitodl/ol-infrastructure/pull/5496) re-keyed the expression
onto `kube_job_failed` in place of a failed-pod count. A rule whose query changed cannot
be put in a before/after firings table; 11 and 102 are not the same measurement.

New since the baseline:

| Rule | Now | Note |
|---|---:|---|
| `APISIXEdge5xxRateFast` | 476 | W4 coverage, working as designed |
| `DagsterPgBouncerConnectionChurnCritical` | 458 | rule deleted 2026-09-19 by #5945, two days before this measurement |
| `DiskUsageCritical` | 148 | see §4 |
| `APISIXOIDCCallbackFailureRateChronic` | 38 | |
| `APISIXEdge5xxRateSlow` | 31 | |
| `DagsterDaemonHeartbeatStaleCritical` | 20 | |

Three of the original four noise rules came down, and `DeploymentUnavailableCritical`
went silent entirely. The fourth, `PodCrashLoopingCritical`, went up, but §3 shows that
is a broken workload rather than a rule that was missed.

Firing counts are alert-instance state transitions, not Rootly
deliveries: 849 `PodCrashLooping` transitions against 624 total Rootly alerts from all
sources and all three stacks. The two are different units and should not be subtracted
from or divided by each other; see §3 for what can and cannot be concluded from that
comparison.

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
`superset-mcp` alone is 710 across four pod names and two ReplicaSets.

Every count in this document is taken over the window ending 2026-09-21T00:00Z. That
matters for this rule specifically, because it is still accumulating: `…-c4ssx` reads 165
to 00:00Z and 231 by 16:00Z the same day. Firing counts for an actively broken workload
are only comparable at a fixed end time.

It is still broken now, on cluster evidence rather than a differently-windowed count:
`kubectl -n superset get pods` shows `superset-mcp-6886bccf8d-c4ssx` in `CrashLoopBackOff`
with 811 restarts.

Its baseline contribution is bounded at 52, since that is what the whole rule fired, and
all four pod names and both ReplicaSets postdate that window, so none of these instances
existed then. This is a workload that broke during the measurement period and has stayed
broken since, which is a `superset-mcp` problem rather than an alerting one.

W3 did treat this rule. [#5503](https://github.com/mitodl/ol-infrastructure/pull/5503)
gave `PodOOMKilled*` and `PodCrashLooping*` the same `keep_firing_for="30m"` and
`missing_series_evals_to_resolve=10`, and added one notification-policy branch matching
`alertname=~"Pod(OOMKilled|CrashLooping)(Warning|Critical)"` that groups both at
namespace level with `group_interval=30m` / `repeat_interval=12h`. Both halves of the fix
cover both rules.

Whether the grouping half is working is not established here. 849 transitions on this
rule against 624 Rootly alerts from every source and all three stacks bounds this rule's
own deliveries at 624, which is weak: it does not show that most transitions were
suppressed, and it does not attribute any suppression to this policy branch rather than
to the root policy or to `keep_firing_for`. The direct measurement is a count of Rootly
alerts carrying `alertname=PodCrashLoopingCritical` over the window with their grouped
instances inspected. That has not been done, and until it is, nothing here should be read
as evidence that W3's grouping works.

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

## 4. `DiskUsageCritical`: 148 state-history instances, but not 148 pages

All 148 firings landed in the last three days of the window, all on one host
(`ip-10-0-3-130`, a Concourse worker) and one device (`/dev/nvme0n1p1`), one per
mountpoint of the form `/var/concourse/worker/volumes/live/<uuid>/volume`. The appendix
query returns 148 series summing to 148, with a single value for both `labels_instance`
and `labels_device`.

The rule in `metric_rules/linux_host.py:106` filters on `device`, not on mountpoint:

```promql
(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 95
```

Concourse mints a live volume per build step, each a separate mountpoint on the same
underlying device, so one worker filling up produces one alert instance per volume
present at the time. The underlying disk pressure is real and already tracked separately
(PR #5933 resizes the infra workers 300GB → 1000GB).

**This does not become 148 pages.** The root notification policy in `alertmanager.py:128`
groups on `instance` and carries no `device` or `mountpoint` label, so every one of these
series shares a notification group and they collapse into a single delivery. The 148 is a
state-history and rule-evaluation cost, not a paging cost, and this section originally
claimed otherwise.

What is left is still worth fixing, but it is smaller than it looks: the rule evaluates
and stores 148 series where one would do, and anything reading the state history (this
document included) has to know to collapse them. If the goal is one instance per worker,
the fix is in the expression rather than the routing, aggregating by `instance` rather
than adding a mountpoint matcher.

`linux_host.py` has had none of W3's storm treatment either way. #5503 named it among the
files still needing it, and the file carries no `keep_firing_for` today.

---

## 5. Grafana Cloud ML: the surface grew 6.5x while nobody was looking

| | Baseline | Now |
|---|---:|---:|
| Forecast jobs, production | 5 | 34 |
| Forecast jobs, QA | 5 | 31 |
| Forecast jobs, CI | — | 0 |
| Outlier detectors, anywhere | 0 | 0 |
| Hand-created jobs of any kind | 0 | 0 |
| Production firings, 30d | 2,812 | 10,138 |

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

None of this reaches a human. The analysis estimated that routing ML unchanged would
roughly triple page volume, on five jobs firing 2,812 times. At 10,138 firings the same
arithmetic gives something closer to an order of magnitude. Do not read that as a ratio
against the 624 Rootly alerts above: §2 says firings and deliveries are different units,
and that applies here too. Grouping would absorb some unknown share of 10,138 exactly as
it absorbs `PodCrashLooping`. The honest statement is that the untuned ML surface
generates more than sixteen times as many firings as the entire human-facing alerting
estate produces deliveries, and nobody has measured what fraction would survive grouping,
because it has never been routed.

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

_The gate is met._ W0-W5 landed, volume is down 57%, the duplicate pipeline is gone,
and the severity split is in place. There is a clean 30-day before/after.

_The pilot as specified is now the wrong next action._ Both the analysis (§6, step 6) and
the spec (W7) scope it as "add 2-3 forecast jobs, Slack only, 30 days". That was written when
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
`courses-backend` learns three-requests-a-day as normal, per the spec's §0.6. A model trained on `superset-mcp`
crashlooping for 30 continuous days learns crashlooping as normal. ML answers "did this
change?", never "is this acceptable?" It supplements W4's absolute-level SLO alerting; it
never replaces it.

_Cost remains the blocker._ Still unquoted, and still the one thing that cannot be
settled from inside the stack. §5 gives the footprint to quote against.

---

## 7. Follow-up work this surfaced

- `superset-mcp` has been crashlooping in production for at least 30 days and is unowned.
  This is the one with a live production impact.
- `keep_firing_for="30m"` does not appear to be holding alerts across a churning pod the
  way #5503 intended. See §3; check it against a workload that is not also broken.
- `DiskUsageCritical` should aggregate its expression by `instance` so one full Concourse
  worker is one series rather than 148. Low priority: §4 shows the root policy already
  collapses these into one notification, so this is evaluation and state-history cost,
  not paging noise. `linux_host.py` was out of scope for W3 regardless.
- The Rootly `noise` field looks untouched. Every alert sampled in this window reads
  `noise: null` or `not_noise`. That is a sample, not a census, but it is consistent with
  W6 not having started.
- Several claims here want a rule-specific Rootly delivery count, which this measurement
  does not have: how many Rootly alerts carried `alertname=PodCrashLoopingCritical`, and
  how many carried `DiskUsageCritical`. Without it, §3 cannot say whether W3's grouping
  is doing the work and §4 cannot quantify what the 148 series actually cost. Worth
  pairing with W6, since both need per-alert attributes rather than totals.

## Appendix: queries used

```
# Rootly window count: binary-search the page index, do not trust the date filters
GET /v1/alerts?page[number]=<n>&page[size]=1     # newest-first; count = page(start) - page(end)

# Firings per rule, 30d, instant query (a range query over [30d] exceeds the Loki limit)
GET /api/datasources/proxy/uid/grafanacloud-alert-state-history/loki/api/v1/query
  query=topk(40, sum by (ruleTitle) (count_over_time({from="state-history"} | json | current =~ `Alerting.*` [30d])))

# Firings per resource, §3. Carries the baseline query's dimensions (analysis appendix)
# so the two are comparable; drop topk() if you need every instance rather than the top N.
  query=topk(25, sum by (ruleTitle, labels_cluster, labels_namespace, labels_pod,
    labels_deployment, labels_statefulset, labels_horizontalpodautoscaler, labels_job_name)
    (count_over_time({from="state-history", folderUID="infrastructure-alerts"} | json
     | current =~ `Alerting.*` [30d])))

# §4's disk breakdown. Unbounded on purpose: topk(25) truncates a 148-instance result,
# which is the whole point of that section.
  query=sum by (labels_instance, labels_mountpoint, labels_device) (count_over_time(
    {from="state-history"} | json | current =~ `Alerting.*`
    | ruleTitle=`DiskUsageCritical` [30d]))

# ML job inventory, per stack
GET /api/plugins/grafana-ml-app/resources/manage/api/v1/jobs
GET /api/plugins/grafana-ml-app/resources/manage/api/v1/outliers

# ML series footprint (regex matchers are rejected on this tenant; name the metric)
GET /api/datasources/proxy/uid/grafanacloud-ml-metrics/api/v1/label/__name__/values
GET /api/datasources/proxy/uid/grafanacloud-ml-metrics/api/v1/query?query=count(<metric>)
```
