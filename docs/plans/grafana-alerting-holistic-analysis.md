# Grafana alerting: holistic analysis and ML evaluation

Date: 2026-08-07
Evidence window: 30 days (2026-07-08 → 2026-08-07)
Sources: Grafana Cloud alert state history (Loki `grafanacloud-alert-state-history`,
production and QA stacks), live rule/routing APIs on all pipelines, Rootly alert API,
production Mimir (`apisix_http_status`), and the Pulumi source in
`src/ol_infrastructure/infrastructure/grafana_alerting/`.

All counts below are measured, not estimated. "Firings" means transitions into the
`Alerting` state recorded in Grafana's own state history.

---

## 1. What is actually deployed

The Pulumi program in `grafana_alerting/` is *one of four* systems currently
generating alerts. Only the first is under version control.

| Pipeline | Rules | Managed by | Routes to |
|---|---|---|---|
| Grafana-managed rules (`infrastructure-alerts`, `log-alerts`) | ~40/stack | Pulumi | Notification policy → Rootly |
| **Mimir + Loki ruler rules** (legacy cortextool era) | **170 metric + 28 log** | **Nothing — orphaned** | **Cloud Alertmanager → Rootly** |
| Synthetic Monitoring rules | 7 | Hand-created in UI | Rule-level receiver override → Rootly |
| Grafana Cloud ML (Adaptive Traces) | 5/stack | Auto-created by plugin | `Adaptive Traces` webhook |

The Pulumi `CLAUDE.md` states Phase 5 retired the cortextool path. The *pipelines* were
retired; **the rules they pushed were never deleted from the rulers and are still being
evaluated and delivered.**

---

## 2. Measured volume

### 2.1 Everything that reached Rootly

**1,449 alerts in 30 days — ~48/day.** Every one is still classified `noise: not_noise`,
i.e. nobody has ever triaged the noise field, so Rootly's own noise tooling has no signal
to work from.

### 2.2 Firings by rule (production stack, 30d)

| Rule | Firings | Delivered to |
|---|---:|---|
| `HTTPRequestDurationTooHighAvg [5m]` | 1,168 | *oblivion (dropped)* |
| `adaptive_traces_forecast_learn_webapp` | 944 | Adaptive Traces webhook |
| `adaptive_traces_forecast_learn_nextjs` | 727 | Adaptive Traces webhook |
| `adaptive_traces_forecast_mitxonline_webapp` | 442 | Adaptive Traces webhook |
| `adaptive_traces_forecast_apisix` | 382 | Adaptive Traces webhook |
| `adaptive_traces_forecast_keycloak_production` | 317 | Adaptive Traces webhook |
| `HPAAtMaxReplicasCritical` | 278 | **Rootly** |
| `PodOOMKilledCritical` | 195 | **Rootly** |
| `ProbeFailedExecutionsTooHigh [5m]` | 55 | *oblivion* |
| `PodCrashLoopingCritical` | 52 | **Rootly** |
| `DeploymentUnavailableCritical` | 26 | **Rootly** |
| `StatefulSetReplicasMissingCritical` | 22 | **Rootly** |
| `Learn NextJS Homepage - Check Failed` | 15 | **Rootly** |
| `Learn API Health Endpoint - Check Failed` | 12 | **Rootly** |
| `KubernetesJobFailedCritical` | 11 | **Rootly** |
| `mitlearn-5xx-error-percentage` | 2 | **Rootly** |
| `mitxonline-5xx-error-percentage` | 2 | **Rootly** |

### 2.3 Firings by rule (QA stack, 30d)

`PodOOMKilledWarning` 296 · `PodCrashLoopingWarning` 185 · `DeploymentUnavailableWarning` 68 ·
`HPAAtMaxReplicasWarning` 35 · `StatefulSetReplicasMissingWarning` 23 ·
`KubernetesJobFailedWarning` 12 · `CeleryBeatPodRestarts` 2 — **621 to Rootly**.
Plus 1,357 Adaptive Traces firings.

### 2.4 The concentration

Grafana-originated Rootly traffic: **615 (prod) + 621 (QA) = 1,236**, the balance being
CloudWatch and the duplicate ruler path.

> **Four rules — `PodOOMKilled`, `HPAAtMaxReplicas`, `PodCrashLooping`,
> `DeploymentUnavailable` — account for 1,135 of 1,236 firings: 92% of everything
> Grafana pages Rootly about.**

Meanwhile **~43 of the ~60 production rules did not fire once in 30 days.** Some of those
are correctly rare (`InvalidAccessKeyProduction`, `edxapp_VaultAuthFailure`). Others are
structurally incapable of firing (§3.5).

---

## 3. Findings — why the noise exists

### 3.1 A complete second alerting pipeline is still live, stale, and unmanaged

The Loki and Mimir rulers still hold the pre-Pulumi rule set, and the **Cloud
Alertmanager has its own route tree with its own `rootly` receiver**. Confirmed by
`GET /api/alertmanager/grafanacloud-ngalertmanager/config/api/v1/alerts`.

Concrete evidence of double-delivery — the same `mitlearn-5xx-error-percentage` event
arriving twice on 2026-08-06:

- `e1S9pM` at 19:38:55.273 — `external_url` is an Explore deeplink (ruler-generated)
- `gzKcPb` at 19:42:30.000 — `external_url` is `/alerting/grafana/efsos1fj7lkw0e/view` (Pulumi rule)

The orphaned copies are not merely duplicates — they have **drifted and are wrong**:

| Rule | Legacy ruler copy | Pulumi copy |
|---|---|---|
| `DeploymentUnavailableWarning` | `cluster=~".*-(production)"` | `cluster=~".*-(ci\|qa)"` |
| `DeploymentUnavailableCritical` | `cluster=~".*-(ci\|qa)"` | `cluster=~".*-(production)"` |

The environment filters are **inverted** — the legacy critical rule pages on CI/QA and
the warning rule on production. Both still use
`kube_deployment_status_condition{condition="Available"}`, the exact query
`eks_general.py:88-102` documents as a false-positive source and deliberately replaced.

Also in the legacy set:

- `CPUUsageWarning` queries `host="$instance"` — an unsubstituted dashboard template
  variable. It can never match. Dead since it was written.
- `DeploymentReplicasMissingWarning` has `severity: "Warning"` (capital W), which does not
  match the route matcher `severity="warning"`, so it silently drops.
- The legacy route tree has an `alertname=~"Deploy.*"` silence the Pulumi tree lacks, and
  groups only by `alertname, environment` — the old grouping that
  `alertmanager.py:99-122` documents as fixed.
- Three Keycloak log rules report `health: "err"` — bare log-stream queries with no
  aggregation. They have never evaluated successfully.

Nine legacy rules (`Daemonset*`, `Disk*`, `Memory*`, `CPUUsage*`) carry a valid
lowercase severity and are **not** covered by the `Deploy.*` silence, so they deliver to
Rootly today.

The same orphaned Loki rules exist on the QA stack, hardcoded to
`cluster="applications-production"` — evaluating production selectors against the QA
tenant.

### 3.2 QA and CI page the production on-call at production urgency

`PodOOMKilledWarning` from `applications-qa` and `data-qa` arrives in Rootly with
`alert_urgency_id: fce5c971…` — **the same urgency as `HPAAtMaxReplicasCritical` from
production.** QA/CI contribute 621 of 1,236 firings: more than half of all Grafana pages
concern non-production environments, at production urgency.

The design intent in `eks_general.py:5-7` is that warning = CI/QA and critical =
production. That is a *severity* distinction, but `alertmanager.py:195-218` routes
`severity=warning` and `severity=critical` to the identical `rootly` contact point. **The
severity label currently has no routing consequence whatsoever.**

### 3.3 Chronic conditions mint a fresh alert per pod

A representative Rootly page (page 37 of 73) contains 18 `PodOOMKilledWarning` alerts —
all the same QA deployment, `mitlearn-default-celery-worker`, cycling at :12 and :19 past
the hour for hours. Each pod replacement produces a new pod name, and because
`group_bies` includes `pod` and `container`, Alertmanager treats each as a new group and
mints a new Rootly alert.

This is the direct cost of the grouping change documented at `alertmanager.py:99-122`.
That change correctly fixed unrelated resources being bundled together, but for a
*churning* workload it converts one persistent problem into an unbounded stream of
alerts. One undersized QA memory limit generated dozens of pages.

The equivalent in production: `keda-hpa-traefik-gateway-controller` fired
`HPAAtMaxReplicasCritical` **122 times in 30 days** (~4×/day) and
`keda-hpa-mitlearn-webapp-scaledobject` 75 times. These are not 122 incidents; they are
one capacity characteristic being re-reported.

### 3.4 UI-created rules bypass the notification policy entirely

There are **two Rootly contact points**: `rootly` (uid `bfsoqo63lsyrka`, Pulumi) and
`Rootly` (uid `eel3rjpiwahoge`, created in the UI). The Synthetic Monitoring rules carry
no `severity` label — under the notification policy they would go to `oblivion` — but they
set `notification_settings.receiver: "Rootly"` directly on the rule, which bypasses the
route tree. Pulumi has no visibility into these.

Two stale OpsGenie contact points also remain, despite `CLAUDE.md` recording OpsGenie as
retired.

The SM rules are also poorly specified. `Learn NextJS Homepage (Bypass Fastly) - Check
Failed` alerts on `avg_over_time(probe_success[5m]) < 1` — **any** single failed probe out
of five pages someone (observed firing at A=0.8, i.e. 4/5 succeeded). Its `summary`
annotation reads *"response times 30% Greater Than Normal"*, which describes latency; the
query measures availability. The annotation is misleading to whoever gets paged.

### 3.5 Rules that cannot fire where they are deployed

Every `*Warning` EKS rule filters `cluster=~".*-(ci|qa)"` but is deployed to all three
stacks. Each Mimir tenant only holds its own environment's clusters, so on the production
stack these rules match nothing, permanently. That is by design per `CLAUDE.md`, and
`no_data_state="OK"` keeps them quiet — but it means **production has no warning tier at
all.** Every EKS alert that fires in production is `critical` and pages. There is no
lower-severity band available for "worth knowing, not worth waking someone."

---

## 4. Findings — what the alerting is missing

This is the more consequential half. The rules are noisy *and* they are watching the
wrong layer.

### 4.1 A 30-day production error regression that never alerted

`api.mitxonline.mit.edu`, 5xx as a percentage of all requests at the APISIX edge, daily:

```
0%  0%  0%  0%  0%  5.5%  9.9%  8.4%  9.4%  11.0%  12.7%  9.4%  7.9%  8.6%  8.1%
2.3%  7.5%  16.1%  17.6%  15.7%  16.7%  16.5%  20.6%  25.3%  24.0%  24.7%  24.0%
21.3%  20.1%  14.0%  18.0%
```

**From 0% to a sustained 18–25%, climbing steadily over four weeks. Zero alerts fired.**
13,403 5xx responses out of 102,011 requests.

`mitxonline-5xx-error-percentage` fired twice in the entire window. It misses this for two
independent reasons:

1. **Wrong measurement point.** It parses the `nginx` sidecar log in the `mitxonline`
   namespace. The failures are visible at the APISIX edge on the `api.mitxonline.mit.edu`
   host, which no rule watches.
2. **Threshold shape.** `> 5%` of *all* traffic for 5 minutes. On a high-volume host a
   serious partial outage rarely crosses a whole-traffic ratio, and a slow creep never
   trips a fixed line at all.

### 4.2 Hosts sitting in chronic double-digit failure

| Host | 5xx rate (30d) | Volume | Alert coverage |
|---|---:|---:|---|
| `opik.ol.mit.edu` | **41.7%** (peaked 90.8%, 79.1% on consecutive days) | 79k | none |
| `courses-backend.learn.mit.edu` | **33.0%** (range 0–48%, chronic) | — | none |
| `api.mitxonline.mit.edu` | **13.1%** and rising | 102k | none |
| `studio.courses.learn.mit.edu` | 1.65% | — | none |
| `api.learn.mit.edu` | 0.15% | 217M | none (304,860 absolute 502s) |

### 4.3 The edge is not monitored at all

APISIX fronts everything and has clean per-host status metrics
(`apisix_http_status{matched_host, code}`). No alert rule uses them. The only HTTP error
alerting is two Loki log-parsing rules covering two namespaces.

### 4.4 Latency signal exists and is discarded

`HTTPRequestDurationTooHighAvg [5m]` fired 1,168 times and every one was dropped to
`oblivion` (no severity label, no receiver override). Its current sample shows
`api.learn.mit.edu/learn/health` averaging **4,141 ms**. A health endpoint taking four
seconds is a real signal that nobody sees. The rule also has `for: 0s`, so it is
maximally flappy in its present form — it needs a duration and a severity, not deletion.

---

## 5. Grafana Cloud ML — evaluation

### 5.1 You are already running it, and it is wired to nothing

The `grafana-ml-app` plugin (v1.36.0) is installed and enabled. Three separate ML/anomaly
systems are already producing alerts:

- **Adaptive Traces forecasts** — 5 auto-created Prophet jobs per stack on per-span
  latency. **2,812 firings in 30 days on production, 1,357 on QA.**
- **Asserts** — `ResourceRateAnomaly`, `LatencyP95Anomaly`, `InboundClientErrorAnomaly`,
  `ErrorLogRateBreach`, and ~20 saturation rules, all live in the Mimir ruler.
- **Synthetic Monitoring** built-ins.

None reach a human. The Adaptive Traces rules route to
`https://tempo-us-central1.grafana.net/adaptive-traces/api/v1/alerts_webhook/…` — that is
Grafana's own sampling-control feedback loop, **not** a notification path. The Asserts
rules carry no `severity` label, so the policy drops them to `oblivion`.

So the real question is not "should we adopt ML alerting" but "should we route what is
already running." The measured answer is **not as-is**.

### 5.2 What the measurement says about out-of-the-box ML quality

`adaptive_traces_forecast_learn_webapp_alert` fired 944 times in 30 days — ~31/day for a
single service. Its condition is:

```
…:predicted{ml_forecast='yhat'} offset 1m
  and …:anomalous offset 1m > 0
  and …:actual offset 1m > 0.050000
```

with `for: 2m`. It fires per `span_name`, so every individual API route is an independent
alert instance, on a 2-minute confirmation window, gated only by an absolute floor of
50 ms. Across five services that is 94 firings/day. Routed to Rootly unchanged, ML would
roughly **triple** current page volume.

That is not an indictment of the technique — it is an untuned model with no severity
grading, no minimum deviation magnitude, and no duration requirement.

### 5.3 Where ML genuinely helps here — and where it does not

**It solves §4.1 and nothing else in §3.** The `api.mitxonline.mit.edu` creep from 0% to
25% is precisely the failure mode a forecast catches and a fixed threshold cannot: the
metric departed from its own learned baseline in week one and never crossed a static line
that would also be quiet in normal operation. Same for the known weekly `mit-learn` RDS
disk-queue spikes — seasonality-aware baselining handles "this is normal on Tuesdays,"
which a threshold cannot express.

**Outlier detection is the right tool for §3.3.** The correct question about
`mitlearn-default-celery-worker` is not "did a pod OOM" but "is *this* pod OOMing unlike
its peers." Outlier detection over a deployment's pods collapses the 296-firing QA storm
into either silence (all peers equally affected — a config problem, not an incident) or a
single genuine outlier.

**It does not help with, and must not be used to paper over:**

- §3.1 duplicate pipeline — a config problem. Adding ML on top of a double-delivering
  Alertmanager doubles the ML alerts too.
- §3.2 QA→Rootly routing — a policy problem.
- §4.2 chronic failure. **This is the important caveat.** An anomaly model trained on
  `courses-backend` at 33% 5xx learns 33% as normal and goes silent. ML answers *"did this
  change?"*, never *"is this acceptable?"* Chronic badness needs an SLO or a threshold.
  Adopting ML without keeping absolute-level alerting would actively hide §4.2.

### 5.4 Cost

Grafana Cloud ML forecasts and outlier detectors are billed per job and consume series in
the ML metrics tenant. `CLAUDE.md` already records that Synthetic Monitoring was rejected
on cost (~$3,200/mo at the desired cadence), so this needs a quote against the current
contract before committing — I have not verified current pricing and would not want a
plan built on a guess.

---

## 6. Recommended sequence

Ordered by benefit-to-risk. Steps 1–3 are cleanup with no new machinery and should land
before any ML work, because ML built on the current routing inherits every defect above.

**1. Delete the orphaned ruler rules and the legacy Cloud Alertmanager config.**
Removes duplicate delivery, the inverted CI/QA↔production filters, the resurrected
`kube_deployment_status_condition` false positive, and the dead `$instance` rule. Highest
value, lowest risk — these are unmanaged, stale, and provably wrong. Verify with
`GET /api/alertmanager/grafanacloud-ngalertmanager/config/api/v1/alerts` afterwards.

**2. Make `severity` mean something.** Route `critical` → Rootly page,
`warning` → Slack or a Rootly low-urgency path. Then either stop routing QA/CI to Rootly
or give it its own low-urgency destination. Expected reduction: ~621 of 1,236 firings
leave the paging path immediately.

**3. Fix the four rules producing 92% of volume.** Not by raising thresholds — by changing
what they ask:
   - Add `keep_firing_for` so flapping resources do not re-page, and consider dropping
     `pod` from `group_bies` for the OOM/crashloop rules specifically (keep `deployment`),
     which collapses the per-pod-name storm without reverting the §3.3 fix wholesale.
   - `HPAAtMaxReplicas`: at-max is a capacity fact, not an incident. Alert on at-max
     **and** a saturation signal (queue depth, latency, error rate), or move it to Slack.
     `traefik-gateway-controller` at 122 firings is telling you to resize the HPA, once.
   - Adopt the existing per-workload exclusion pattern (as already done for
     `keda-hpa-mitxonline-hubspot-sync-celery-worker`) for known-benign chronic cases,
     each with a linked issue so exclusions do not become permanent by default.

**4. Add edge-level SLO alerting on APISIX.** This is the single biggest coverage win and
needs no ML:
```promql
sum by (matched_host) (rate(apisix_http_status{code=~"5.."}[10m]))
  / sum by (matched_host) (rate(apisix_http_status[10m]))
```
Multi-window burn-rate (fast: 5% over 10m; slow: 1% over 6h) catches both the `opik` cliff
and the `api.mitxonline` creep. Start it warning-only for two weeks to calibrate against
the chronic offenders in §4.2 before it pages.

**5. Give `HTTPRequestDurationTooHighAvg` a `for:` duration and a `severity`,** or replace
it with an explicit latency SLO. It is currently discarded signal, and it is reporting a
4.1-second health endpoint.

**6. Then, and only then, pilot ML — narrowly.** Two or three forecast jobs on metrics
where a *change* is the thing you care about and seasonality defeats thresholds:
   - APISIX 5xx ratio per host (complements, does not replace, step 4's SLO)
   - `mit-learn` RDS disk queue depth
   - one outlier detector over `mitlearn` celery worker pod memory

Route the pilot to Slack, not Rootly, for at least 30 days. Measure firings against known
incidents before promoting anything to a page. The 944-firing Adaptive Traces job is the
control group for what happens if you skip that step.

**7. Start using Rootly's `noise` field.** All 1,449 alerts are unclassified. Marking
noise is what makes Rootly's grouping and suppression useful, and it produces the data to
justify the next round of tuning.

---

## Appendix: verification commands

```
# Duplicate ruler rules (should be empty after step 1)
GET /api/alertmanager/grafanacloud-ngalertmanager/config/api/v1/alerts

# Firings per rule, per stack, 30d — the core measurement
# datasource: grafanacloud-alert-state-history
sum by (ruleTitle) (count_over_time({from="state-history"} | json | current =~ `Alerting.*` [30d]))

# Firings per resource, to find chronic offenders
topk(30, sum by (ruleTitle, labels_cluster, labels_namespace, labels_horizontalpodautoscaler,
  labels_pod, labels_deployment, labels_statefulset, labels_job_name)
  (count_over_time({from="state-history", folderUID="infrastructure-alerts"} | json
   | current =~ `Alerting.*` [30d])))

# Edge 5xx ratio by host — the coverage gap
100 * sum by (matched_host) (increase(apisix_http_status{code=~"5.."}[30d]))
    / sum by (matched_host) (increase(apisix_http_status[30d]))
```
