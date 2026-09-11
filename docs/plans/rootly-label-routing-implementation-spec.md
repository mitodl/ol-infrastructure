# Connecting the `ol.mit.edu` label hierarchy to Rootly — implementation spec

**Date:** 2026-08-17
**Supersedes** §7 (proposed scheme), §8 (recommended sequence) and §9 (open questions)
of [rootly-labeling-and-alert-routing-analysis.md](rootly-labeling-and-alert-routing-analysis.md).
The analysis's *measurements* stand except where §0 below corrects them; its
*recommendations* are replaced by this document.

Every claim in §0 was verified against live state on 2026-08-17: the production
Mimir tenant, the production Grafana provisioning API, the Rootly API, the
`k8s-monitoring` chart 4.4.0 sources and JSON schema, and the repo at `f44406fa8`.

**Companion document.** [grafana-alerting-remediation-spec.md](grafana-alerting-remediation-spec.md)
(2026-08-07) owns the alert-rule-quality and Rootly-hygiene workstreams W0–W7. This
spec covers all three phases of the label project, so §2 restates the Phase 1 items
that overlap it — but as *status*, deferring the implementation detail to that spec.

Precedence, narrowly: that spec is authoritative for **W0** (enabling the two disabled
CI/QA route rules) and **W2a** (the CI source's High default urgency), both still open.
This one is authoritative for anything touching a label. **Its W2b is superseded and
should not be implemented** — it proposes adding a Low-urgency deferral path on the
grounds that Low has no non-paging path, which was true on 2026-08-07 and is not now:
PRs #5354 and #5377 shipped `escalation_path_defer_low_urgency_off_hours` and
`escalation_path_medium_urgency_slack_only`, giving both bands a Slack-only destination
(§0.1). Building W2b on top would add a second, redundant path over the same urgency.
Likewise its §0.7c and W5b describe Synthetic Monitoring receiver overrides that
PR #5400 has since removed — see §0.4.

---

## 0. Re-baseline: what changed, and three findings that change the plan

### 0.1 Phase 1 is mostly done. The label blockers are untouched.

| Analysis finding | Status on 2026-08-17 |
|---|---|
| §5.6 six routes UI-managed, ~51 rules unmanaged | **Closed.** PR #5218 adopted the Service Routes; all **12** routes are now `rootly.AlertRoute` resources in `saas/rootly/__main__.py`, `sh-test` included |
| §5.2 QA/CI Slack diversion inert | **Open.** Both fallback rules still `enabled: false`; `pulumi_rootly`'s `AlertRouteRuleArgs` has no `enabled` field, so this is unmanageable from Pulumi (remediation spec W0) |
| §5.2 Low urgency pages like High | **Closed.** PR #5354 added `escalation_path_defer_low_urgency_off_hours` (`__main__.py:729`); PR #5377 added `escalation_path_medium_urgency_slack_only` (`:758`) routing Medium and Low to `#devops-warnings` |
| §5.7 `Cloudwatch - Critical` `setup_incomplete` | **Open.** Still `setup_incomplete` live |
| §5.7 `exampleDeleteMe-EscalationPolicy` | **Open.** Still declared at `__main__.py:538` |
| §4.4 11-entry alertname demotion allowlist | **Unchanged.** All 12 urgency rules present on source `90cda8ea`, created 2026-07-20, none since |
| §1 kube-state-metrics exports no labels | **Unchanged.** `clusterMetrics` and `telemetryServices["kube-state-metrics"]` in `substructure/aws/eks/grafana.py:379,501` carry no label configuration |
| §3.2 schema defects | **Unchanged.** `Component` still four members; `product`/`application`/`component` still only on `K8sAppLabels`; `Services.learn_ai` still a tuple; `BusinessUnit.residential_staging` still present |

Live re-verification of the core blocker, production Mimir:

```
count(kube_pod_labels) or vector(-1)                        →  -1
__name__ values matching kube_(pod|deployment|statefulset|namespace)_(labels|info|annotations|owner)
                                                            →  ["kube_pod_info", "kube_pod_owner"]
```

`kube_pod_info` and `kube_pod_owner` are present, so kube-state-metrics *is* being
scraped and reaching Mimir. The `*_labels` metrics are absent entirely.

### 0.2 [New] There are **two** independent gates, not one — and the analysis only found the first

**Gate 1 — kube-state-metrics emits nothing at all when the allowlist is empty.**
From the generator, `internal/store/pod.go`:

```go
func createPodLabelsGenerator(allowLabelsList []string) generator.FamilyGenerator {
    ... wrapPodFunc(func(p *v1.Pod) *metric.Family {
        if len(allowLabelsList) == 0 {
            return &metric.Family{}          // <-- no series for any pod
        }
        labelKeys, labelValues := createPrometheusLabelKeysValues("label", p.Labels, allowLabelsList)
        m := metric.Metric{LabelKeys: labelKeys, LabelValues: labelValues, Value: 1}
        return &metric.Family{Metrics: []*metric.Metric{&m}}
    })
}
```

So the absence of `kube_pod_labels` from Mimir today is fully explained by the missing
`--metric-labels-allowlist`, and today's live state tells us nothing about gate 2 either
way. **Note also the second branch: once the allowlist *is* configured, a series is
emitted for every pod with `Value: 1`, whether or not that pod carries any allowlisted
label.** An unlabeled workload therefore gets a join partner with the join keys and no
`label_ol_mit_edu_*` columns — which is what §4 depends on, and why the coverage gate
is about routing quality rather than alert availability.

**Gate 2 — the chart drops the metric post-scrape**, independently and verifiably.
In `k8s-monitoring` 4.4.0,
`clusterMetrics.kube-state-metrics.metricsTuning.useDefaultAllowList` defaults to
`true` (`charts/feature-cluster-metrics/values.yaml:676`), and the default allow-list
(`charts/feature-cluster-metrics/default-allow-lists/kube-state-metrics.yaml`)
contains exactly one `*_labels` metric:

```
kube_persistentvolumeclaim_labels
```

No `kube_pod_labels`, no `kube_deployment_labels`, no `kube_statefulset_labels`,
no `kube_daemonset_labels`. Alloy drops them post-scrape, before remote_write.

**Consequence:** the analysis's §7.3 Option A, applied as written, produces
kube-state-metrics series carrying `label_ol_mit_edu_*` columns that Alloy then
throws away. The Mimir tenant would look exactly as it does today and the PromQL
joins would still drop every series. Both of these are required:

| Gate | Where | Effect if omitted |
|---|---|---|
| `metricLabelsAllowlist` | `telemetryServices["kube-state-metrics"]` | no `kube_*_labels` series produced at all |
| `metricsTuning.includeMetrics` | `clusterMetrics["kube-state-metrics"]` | series produced but dropped by Alloy before remote_write |

Both failure modes look identical from Mimir — the metric is simply absent — so there
is no query that distinguishes them from outside. The only way to tell gate 2 is still
closed is to apply gate 1 and observe that `kube_pod_labels` is *still* missing.
Apply both together and check once.

### 0.3 [New] The §7.3 code sketch has the wrong values path, and would silently no-op

The analysis proposed:

```python
"telemetryServices": {
    "kube-state-metrics": {
        "deploy": True,
        "kube-state-metrics": {          # <-- extra nesting level
            "metricLabelsAllowlist": [...],
        },
    },
}
```

`charts/telemetry-services/values.schema.json` defines
`properties["kube-state-metrics"]` with exactly these properties: `autosharding`,
`deploy`, `metricLabelsAllowlist`, `nodeSelector`, `podAnnotations`,
`prometheusScrape`, `releaseLabel`, `updateStrategy`. `metricLabelsAllowlist` is a
**flat array of strings** one level down, not nested under a repeated key. The schema
does not set `additionalProperties: false`, so the extra level is **silently
ignored** — no Helm error, no Pulumi diff signal, and the value never reaches the
kube-state-metrics Deployment's `--metric-labels-allowlist` flag.

**Second trap in the same value.** The chart already ships a non-empty default:

```yaml
metricLabelsAllowlist:
  - nodes=[agentpool,alpha.eksctl.io/cluster-name,...,topology.kubernetes.io/zone]
```

Helm replaces lists rather than merging them. Supplying only `pods=[...]` silently
drops 20 node labels that the Grafana Cloud Kubernetes integration's node views
depend on. The `nodes=[...]` entry must be carried forward verbatim.

### 0.4 Q4 resolved: the `Grafana` source is now inert — and two of its dead rules can be made live today, without Phase 3

Analysis Q4 asked what still delivers to alert source `f4d836c0`
(`source_type: "grafana"`). It *was* fed by the three MIT Learn Synthetic Monitoring
rules, which set `notification_settings.receiver: "Rootly"` — the UI-created duplicate
contact point (`eel3rjpiwahoge`, posting to the `grafana_webhooks` endpoint with a
token matching that source), distinct from Pulumi's `rootly` (`bfsoqo63lsyrka`, the
`alertmanager_webhooks` endpoint).

**PR #5400 (2026-08-13) removed that override**, adopting all three rules into
`metric_rules/synthetic_monitoring.py` and routing them through `alertmanager.py`'s
policy tree instead. Confirmed applied — production `/api/v1/provisioning/alert-rules`
for folder `grafana-synthetic-monitoring-app`:

| Rule | receiver | labels |
|---|---|---|
| `Learn Homepage - Check Failed` | `policy-tree` | `component=webapp`, `service=mitlearn`, `severity=critical` |
| `Learn API Health Endpoint - Check Failed` | `policy-tree` | `component=api`, `service=mitlearn`, `severity=critical` |
| `Learn NextJS Homepage (Bypass Fastly) - Check Failed` | `policy-tree` | `component=nextjs`, `service=mitlearn`, `severity=warning` |

(plus three `- Elevated Probe Failure Rate` rules #5400 added, same labels, no severity.)

So source `f4d836c0` now has **no feed at all**, and the "Grafana Service Route" bound
to it is genuinely dead — which is what analysis §5.5 suspected. This also supersedes
remediation spec §0.7c, which still describes the three receiver overrides as live;
deleting the duplicate `Rootly` contact point (its W5b) is now safe.

**The three `component` rules must still be moved to the Grafana Production Service
Route (bound to `90cda8ea`) — and the reason is now much better than "so they work
after Phase 3".** Those SM alerts route through the Pulumi contact point to the
production `alertmanager` source *today*, carrying exactly the payload fields those
rules test:

| Rule | Condition | Matches today? |
|---|---|---|
| MIT Learn API → `MIT Learn - API` (`3ad10823`) | `service contains "mitlearn"` AND `component contains "api"` | **yes** |
| MIT Learn NextJS → `MIT Learn - NextJS` (`b2389961`) | `service contains "mitlearn"` AND `component contains "nextjs"` | **yes** |
| MIT Learn AI API → `MIT Learn AI - API` | `service contains "learn-ai"` AND `component contains "api"` | no — no rule emits `service=learn-ai` |

**Two of the 19 orphaned Rootly services (§5.3) become reachable the moment those two
rules are moved.** No label pipeline, no kube-state-metrics change, no Phase 2. This is
the cheapest item in the whole project and it is available now — see P1.5 in §5.

Caution on ordering: the `Check Failed` rules carry `severity`, the `Elevated Probe
Failure Rate` rules do not, so the latter drop to `oblivion` in the policy tree. Only
the three `Check Failed` rules actually arrive at Rootly to be routed.

### 0.5 [New] The join target differs per rule, and two rule pairs cannot be joined at all

`metric_rules/eks_general.py` holds **20** rules (10 warning/critical pairs), and they
do not all aggregate at pod granularity. The join target follows the aggregation key:

| Rule pair | `sum by (…)` key | Join against | Feasible? |
|---|---|---|---|
| `PodOOMKilled*`, `PodCrashLooping*` | `cluster, namespace, pod, container` | `kube_pod_labels` | yes |
| `CeleryBeatPodRestarts*` | pod-level (`increase(...)`) | `kube_pod_labels` | yes |
| `DeploymentReplicasMissing*`, `DeploymentUnavailable*` | `cluster, namespace, deployment` | `kube_deployment_labels` | yes |
| `StatefulSetReplicasMissing*` | `cluster, namespace, statefulset` | `kube_statefulset_labels` | yes |
| `DaemonsetReplicasMissing*` | `cluster, namespace, daemonset` | `kube_daemonset_labels` | yes |
| `KubernetesJobFailed*` | `cluster, namespace, job_name` | `kube_job_labels` | yes, but Job labels are set by the launching controller |
| `HPAAtMaxReplicas*` | `cluster, namespace, horizontalpodautoscaler` | — | **no** |
| `NodeNotReady*` | `cluster, node` | — | **no** (not a workload) |

`HPAAtMaxReplicas*` is the important negative. The HPAs are created by KEDA
(`keda-hpa-<scaledobject>`), so they carry KEDA's labels, not ours, and
`kube_horizontalpodautoscaler_labels` would not contain `label_ol_mit_edu_*` no
matter what is allowlisted. The `scaleTargetRef` that would bridge HPA → Deployment
is not exposed as a metric. Deriving the workload by string-stripping the
`keda-hpa-` prefix is the only join available and it is exactly the kind of
name-substring coupling this project exists to remove.

**Decision (§1.5):** `HPAAtMaxReplicas*` and `NodeNotReady*` are out of scope for
tier-based urgency. HPA-at-max moves off the paging path wholesale via remediation
spec W3c; `NodeNotReady` stays a node-level page.

So Phase 3 rewrites **16 of 20** rules, not "the 18 EKS rules" as §8 step 11 said,
and needs **five** allowlisted resource kinds, not three.

### 0.6 [New] Cardinality: sized, and one cluster dominates

Production pod counts, live:

| Cluster | Pods |
|---|---:|
| `data-production` | **2,004** |
| `applications-production` | 363 |
| `residential-production` | 124 |
| `operations-production` | 115 |
| **Total** | **2,606** |

`kube_pod_labels` is one series per pod, so allowlisting `pods` adds ~2,600 active
series — a modest delta against `kube_pod_info`/`kube_pod_owner`, which already exist
at that same cardinality. The real cost is **churn**, not count: `data-production`'s
2,004 pods are overwhelmingly Dagster job pods, which mint a new pod name per run.
Every new pod name is a new `kube_pod_labels` series.

Workload-level series are cheap and stable by comparison — 262 Deployments/
StatefulSets/DaemonSets across the three clusters the analysis surveyed.

**Decision (§1.6):** allowlist `deployments`, `statefulsets`, `daemonsets` (stable,
~262 series) plus `pods` (needed for the OOM/crashloop pair, which is 70% of alert
volume), and exclude the `dagster` namespace from the pod-level series via
`extraMetricProcessingRules` so the churn is not paid for. Dagster job pods are
already excluded from `KubernetesJobFailed*` in PromQL (`namespace!="dagster"`) and
are tiered `ticket`, so no signal is lost.

### 0.7 [New] Making `Component` strict breaks nothing — there is exactly one call site

A `grep` for `component=` across `src/` returns five hits, but only one of them is the
`K8sAppLabels` field:

| Hit | What it actually is |
|---|---|
| `mit_learn_nextjs/__main__.py:49` `component="frontend"` | **the only `K8sAppLabels.component` call site** — and `frontend` is already in the enum |
| `synthetic_monitoring.py:206,229,245` `component="nextjs"/"api"/"webapp"` | the `_Check` dataclass's own `component: str` field — a Grafana *alert label*, unrelated to the K8s label classes |
| `dagster/__main__.py:947` `component=pgbouncer` | inside a **comment**, describing a raw Kubernetes Service label used as a `ServiceMonitor` selector |

So dropping `| str` from `Component | str | None` breaks **zero** call sites, and the
three-commit ordering is not forced by breakage. Extending the enum first is still the
right order — a strict enum with four members would reject `api`/`nextjs` the moment
anyone sets them, and those values are already in live use as alert labels (§0.4) — but
it is a design choice, not a migration constraint. §3.1 can be two commits.

Note the corollary for §4.5: `component` as a Rootly routing key already has two
distinct producers with no shared vocabulary — `synthetic_monitoring.py`'s `_Check`
and (after Phase 3) the `ol.mit.edu/component` label. They agree on `api`, `nextjs` and
`webapp` today by coincidence, not by construction. Making `_Check.component` typed as
`Component` is a one-line change that turns that coincidence into a guarantee, and is
worth doing in the same commit as the enum extension.

---

## 1. Decisions

Recorded here so they stop being open questions. Q2 was resolved 2026-07-30 (analysis
§8.1) and is unchanged.

**1.1 Q1 — CI alerts keep reaching Rootly.** Do not drop them to `oblivion` in the CI
Grafana stack's notification policy. Rationale: `setup_grafana()` returns early for CI
(`grafana.py`), so CI clusters ship no metrics and the CI rules evaluate to `NoData` —
the remediation spec measured 219 `Normal (NoData)` states and **zero** genuine
`Alerting` states in 30 days. The hazard is not volume, it is the CI source's default
urgency of **High**, which is a live trap if CI monitoring is ever enabled. Fix the
urgency (remediation spec W2a: High → Low) and keep the delivery path visible in
Rootly where it can be audited, rather than hiding it behind a Grafana-side drop.
Corollary: the `ci` half of every `cluster=~".*-(ci|qa)"` filter in `eks_general.py`
is dead and should be left alone rather than "fixed" — it costs nothing and documents
intent.

**1.2 Q3 — stateful services stay at `notify`** *(tmacey, 2026-08-17)*. The demotion of
`StatefulSetReplicasMissingCritical` was deliberate, not collateral. MySQL, MongoDB,
OpenSearch, Qdrant, ClickHouse and StarRocks are tiered `notify`, preserving today's
behaviour but expressing it per-workload instead of per-alertname.

*This materially shrinks Phase 3's headline benefit and the spec says so plainly.*
Analysis §7.5 projected escalating stateful and ingress workloads back to `page`; with
stateful staying at `notify`, the only escalation left is ingress/APISIX and webapp
OOMs. Phase 3's net paging-volume delta is close to zero. What it still buys:

1. **Removes the hand-maintained allowlist** (analysis G5). Eleven alertnames in
   Rootly conceptually coupled to `eks_general.py`, where adding a rule in one place
   silently defaults to High in the other.
2. **Fixes §5.4.** `mitx-staging-ts-sts` in `residential-production` receives Critical
   severity because its *cluster name* ends in `-production`. A declared
   `ol.mit.edu/environment` fixes this; nothing else can.
3. **Reaches the 19 orphaned Rootly services** (analysis §5.3), every Celery service
   among them, which no routing rule can currently target.
4. **Makes per-workload exceptions a manifest edit** rather than a Rootly UI edit, and
   gives the "this workload is expected to OOM" intent (analysis G2) somewhere to live
   other than a code comment.

If the goal were paging volume alone, Phase 1's W0 is where that win is — up to
621 alerts/30d — and Phase 3 would not be worth its cost. The justification for
Phase 3 is routing precision and maintainability.

**1.3 Q5 — keep `BusinessUnit` and `Product`, with distinct jobs.** `ou` remains the
cost-allocation axis (already required on every AWS resource via `AWSBase`); `product`
becomes the alerting/ownership roll-up that maps onto the Rootly service catalog,
which is named by product. No enum collapse. One fix: drop
`BusinessUnit.residential_staging` (analysis §3.2e) — staging is an environment, and
having it in `BusinessUnit` splits one OU's cost roll-up in two.

**1.4 Q6 — central backfill by the infrastructure team, plus a CI gate.** Infra owns
most of the 205 unlabeled workloads anyway (`operations-production` 97% unlabeled,
`data-production` 96%), and coordinating with application teams for the remaining
`applications-production` gap would add latency to the long pole. A pre-commit/CI
check then fails any new K8s workload constructed without the required labels, so the
gap cannot reopen. Phase 2 is weeks, not quarters.

**1.5 Join scope** (from §0.5). Tier-based urgency covers 16 of the 20
`eks_general.py` rules. `HPAAtMaxReplicas*` is excluded — no joinable label exists on
a KEDA-created HPA — and moves off the paging path via remediation spec W3c.
`NodeNotReady*` is excluded as a node-level, not workload-level, condition.

**1.6 Series scope** (from §0.6). Allowlist `pods`, `deployments`, `statefulsets`,
`daemonsets`, `jobs`. Exclude the `dagster` namespace from pod-level label series to
avoid paying for Dagster's job-pod churn.

**1.7 Label rename at the rule boundary.** Alert rules emit a clean `alert_tier` /
`ol_component` / `ol_service` / `ol_environment` label rather than surfacing
`label_ol_mit_edu_alert_tier` into Rootly. `label_replace()` in the rule expression
does this, so Rootly conditions read `$.commonLabels.alert_tier` and stay legible.
The `label_ol_mit_edu_*` form only ever appears inside PromQL.

---

## 2. Phase 1 — status and the four items that remain

Implementation detail for W0 and W2 lives in the remediation spec; this section exists
so the label project has one accurate status view and so the Phase 3 prerequisites are
explicit.

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | Enable the two disabled CI/QA route fallback rules (route `691913ad` rule `1e552e8f`; route `588edbfc` rule `53e8784f`) | remediation W0 | **open** — UI-only; `AlertRouteRuleArgs` has no `enabled` field |
| 1a | CI source default urgency High → Low (`__main__.py:3013`) | remediation W2a | **open** |
| 1b | `QA Non-Paging Escalation Policy` (`d63b7456`) has one service and zero levels — those CloudWatch alarms notify nobody | this project | **open** — delete the policy and move `MITx Online QA - Open edX - Redis` onto `CI/QA Slack Notifications` |
| 1c | Account-wide "default alerts channel" toggle posts every alert to `#devops-alerts` regardless of routing (`__main__.py:513-521`) | this project | **open** — until this is off, the CI/QA separation has no observable effect |
| 2 | Delete `exampleDeleteMe-EscalationPolicy` (`__main__.py:538`); finish `Cloudwatch - Critical` source setup | this project | **open** |
| 3 | Three dead `component` rules in the Grafana Service Route | this project | **re-specified and promoted** — *move* to the Grafana Production Service Route, don't repair in place; two of the three match live payloads **today** (§0.4), so this is step **P1.5**, not a Phase 3 item. Delete the now-inert Grafana Service Route with them |
| 4 | Import the six unmanaged alert routes | — | **closed** by PR #5218 |
| 5 | Low urgency pages like High | — | **closed** by PRs #5354, #5377 |

**Prerequisite relation to Phase 3:** none of Phase 3 depends on Phase 1. Item 1c is a
prerequisite for *observing* whether any routing change worked, so it should land
before Phase 3's verification step, not before its implementation.

---

## 3. Phase 2 — schema and coverage

### 3.1 Schema changes, in dependency order

Two commits. §0.7 establishes that nothing currently assigns a non-enum value to the
typed field, so the type can be tightened in the same commit that extends the enum.

**Commit A — extend `Component`, and tighten the type.**

```python
@unique
class Component(StrEnum):
    """Functional role of a workload within its service.

    The discriminator for paging decisions: a Celery worker and a Postgres
    StatefulSet fail differently and should not share an alert tier.
    """

    api = "api"
    beat = "beat"
    cache = "cache"
    celery = "celery"
    database = "database"
    frontend = "frontend"
    gateway = "gateway"
    ingress = "ingress"
    keycloak = "keycloak"
    nextjs = "nextjs"
    pgbouncer = "pgbouncer"
    queue = "queue"
    search = "search"
    webapp = "webapp"
    worker = "worker"
```

`api`, `nextjs` and `webapp` are present because `synthetic_monitoring.py`'s `_Check`
already emits them as Grafana alert labels (§0.4); `pgbouncer` because Dagster uses it
as a raw Kubernetes Service label. `gateway` and `queue` are added for APISIX/Traefik
and RabbitMQ/Redis-as-broker, which have no current member and are exactly the
wide-blast-radius workloads in `operations-production`.

Same commit: `component: Component | None` (dropping `| str`), and retype
`_Check.component` from `str` to `Component` so the two producers of Rootly's
`component` routing key share one vocabulary by construction rather than by
coincidence (§0.7).

**Commit B — add `alert_tier`, and move the roll-up fields into the base class.**
Landed 2026-08-20, with two corrections to the sketch below.

```python
@unique
class AlertTier(StrEnum):
    """Paging eligibility for alerts about this workload."""

    page = "page"  # wake someone: user-facing or data-integrity impact
    notify = "notify"  # business-hours attention: degraded, self-healing, or redundant
    ticket = "ticket"  # record only, never notifies


class K8sGlobalLabels(BaseModel):
    ou: BusinessUnit
    service: Services
    stack: StackInfo
    # Moved down from K8sAppLabels: a base-class-labeled resource must still be
    # rollable-up to a product, and 26 of the 39 label call sites use this class.
    product: Product | None = None
    application: Application | None = None
    component: Component | None = None
    alert_tier: AlertTier | None = None
    # Overrides the value derived from stack.env_suffix in model_dump. A workload
    # whose logical environment differs from its cluster's -- mitx-staging-* in
    # residential-production -- must be able to say so; today it silently inherits
    # the cluster's answer and is treated as production. See analysis section 5.4.
    environment: DeploymentEnvironment | None = None
```

`model_dump` changes one line: `ol.mit.edu/environment` becomes
`self.environment or self.stack.env_suffix`. Every existing call site is unaffected —
all four new fields default to `None` and `exclude_none=True` already drops them.

`K8sAppLabels` keeps `pod_security_group`, `source_repository`, `commit_sha`,
`release_tag` and re-declares `product`, `application` and `source_repository` as
required, so it stays the stricter contract it is today.

**Correction 1: `environment` cannot be typed `Environment`.** That enum's members
are `applications`, `data`, `mitx`, `mitxonline`, `mitx-staging`, `operations`,
`xpro` — it names the VPC a resource lives in. The label it would be overriding is
`stack.env_suffix`, whose values are `ci`, `qa`, `production`, `dev`, plus the `rc`
spelling ODL Video and the Pingdom checks use for QA. Typing the override against
`Environment` would have made the field unable to express any value the label
actually takes. A separate `DeploymentEnvironment` enum carries the stage, and both
enums now carry a docstring saying which axis they are.

**Correction 2: the `| str` escape hatches stay closed.** Commit A removed
`Component | str`; re-adding it here would undo that in the same phase.

**What honoring the fields cost, which the sketch does not mention.** Four call
sites — Airbyte, Dagster, JupyterHub, Kubewatch — were already passing `product`
and `application` to `K8sGlobalLabels`, where pydantic discarded them. `mypy` had
been reporting all four as `Unexpected keyword argument` against a 624-error
baseline, so nothing surfaced them. All four also pass `source_repository`, so all
four met `K8sAppLabels`' full contract and were switched to it.

Making those values take effect adds labels to rendered resources, and two of them
landed in selectors. Dagster spread the whole rendered label dict into
`spec.selector.matchLabels` on the pgbouncer and sql-exporter Deployments and into
`spec.selector` on their Services. A Deployment selector is immutable, so every
future label addition — including the `alert_tier` and `component` backfill in §3.3
— would have demanded a delete-and-recreate of the Deployment that fronts every
database connection the data platform makes. A Service selector is mutable but has
to agree with the pod labels, and nothing orders the Service patch after the
Deployment finishes rolling; in between, the Service has no endpoints. Both are now
frozen to the four keys the live selectors already carry.

**Before the §3.3 backfill touches any workload, check it the same way:** a label
set that is about to grow must not be the source of a selector. `pulumi preview`
finds these — the Service selector above did not appear in any grep for
`matchLabels`, only in the preview diff.

### 3.2 Enum hygiene, same phase

- `Services.learn_ai = ("learn-ai",)` → `"learn-ai"` (analysis §3.2d). Harmless today
  because `StrEnum` splats the tuple, which is precisely why it should be fixed before
  anyone relies on the enum base.
- Drop `BusinessUnit.residential_staging` (§1.3). Audit `AWSBase` callers tagging
  `OU=residential-staging` first; they move to `residential` with the environment
  carried by the `Environment` tag.
- Remove `Services.open_edx` **or** `Services.openedx` — having both as distinct
  members is a coin-flip for anyone labeling an Open edX workload (analysis §3.2c).
- Do **not** reconcile `Services` against `Application` wholesale. §1.3 keeps them as
  separate axes; the drift the analysis catalogued matters only if one is derived from
  the other, which is no longer proposed. Add a class-level docstring to each stating
  what it is for, so the next reader does not re-derive the question.

### 3.3 Coverage backfill (the long pole)

Order by blast radius, not by count. Re-measured 2026-08-20 against pod-template
labels — the ones `kube_pod_labels` reads, not the workload's `metadata.labels`:

| cluster | workloads | `service` | `component` | `alert_tier` |
| --- | --- | --- | --- | --- |
| `operations-production` | 66 | 7 (10%) | 0 (0%) | 0 |
| `data-production` | 93 | 15 (16%) | 1 (1%) | 0 |
| `applications-production` | 113 | 53 (46%) | 46 (40%) | 0 |
| `residential-production` | 71 | 24 (33%) | 18 (25%) | 0 |

1. **`operations-production`** — the widest-blast-radius workloads in the estate and
   the worst-covered: APISIX, cert-manager and the shared ingress. Tier `page`,
   `component` = `gateway`/`ingress`. Note that `Services` has no member for any of
   them, so this backfill starts by extending that enum, not by labeling.
2. **`data-production`** — mostly Dagster and pipeline workloads; tier `ticket` or
   `notify`. High count, low paging stake.
3. **`applications-production`** and **`residential-production`** — highest paging
   stake per workload. The 71 already carrying `component` need `alert_tier` added,
   not labels from scratch.

`alert_tier` is at zero everywhere, by construction: the field landed 2026-08-20 and
nothing sets it yet. That is the number §3.5's gate is measured against, and the
reason Phase 3 stays blocked — at today's coverage a `group_left` rewrite would
silence most of EKS alerting rather than re-tier it.

Constructing a label object is not the same as labeling anything. Kubewatch builds a
full `K8sAppLabels` and then never passes it to a resource; `pulumi preview` on that
stack reports zero changes. The §3.4 gate is written against rendered resources for
exactly this reason.

#### 3.3.1 [2026-08-28] `operations-production` is Helm charts, and the values key is per chart

Every workload in P2.C's step 1 is installed from a third-party chart, so none of them
constructs a label model and the §3.4 gate cannot reach them. What a chart accepts is
the whole problem, and it varies: the key has to land on the **pod template** (that is
what `kube_pod_labels` reads) and must not land in `spec.selector`, which is immutable
on a Deployment — a label that reaches the selector forces a delete-and-recreate of the
addon rather than a rolling restart.

Verified by rendering each chart at its pinned version with a sentinel label under each
candidate key, then by `pulumi preview` against `operations.Production` (10 updates,
0 replacements; the two non-Helm diffs are pre-existing AMI drift):

| Chart (pinned version) | Key that reaches the pod | Also on the workload object |
|---|---|---|
| `cert-manager` v1.21.1 | `global.commonLabels` | same key |
| `external-dns` 1.21.1 | `podLabels` | `commonLabels` |
| `traefik` 41.4.0 | `commonLabels` | same key |
| `vault-secrets-operator` 1.5.1 | `controller.extraLabels` | — |
| `vertical-pod-autoscaler` 0.11.0 | `<component>.podLabels` | `commonLabels` |
| `aws-load-balancer-controller` 3.5.0 | `podLabels` | `additionalLabels` |
| `metrics-server` 3.13.x | `podLabels` | `commonLabels` |
| `aws-node-termination-handler` 0.27.2 | `podLabels` | `customLabels` |
| `karpenter` 1.14.1 | `podLabels` | `additionalLabels` |
| `keda` 2.20.2 | `podLabels.<component>` | `additionalLabels` (one set for all three) |
| `vantage-kubernetes-agent` 1.9.5 | `podLabels` | `appLabels` |
| `dcgm-exporter` 4.8.3 | `podLabels` | — |

**Three findings from doing it.**

1. **The vault-secrets-operator release was labeling nothing.** It passed the shared
   label dict to a top-level `extraLabels`, and the chart has no such key — only
   `controller.extraLabels`. Helm does not error on an unrecognized value, so the
   labels were silently dropped for as long as that line has existed. Any chart wired
   by analogy rather than against its `values.yaml` can be wrong the same way, and
   nothing in the pipeline says so.
2. **Two addons cannot be labeled through their charts at all.** `apisix` 2.16.1
   exposes only `service.labelsOverride`, which *replaces* `apisix.selectorLabels` —
   used for the selector and the pod template both, so any label added there rewrites
   the immutable selector. `nvidia-device-plugin` 0.20.0 has only
   `selectorLabelsOverride`, the same trap. APISIX is the single widest-blast-radius
   workload in the estate, so it needs the `DeploymentPatch` route (the pattern
   already in `core_dns.py`) rather than a chart value.
3. **EKS-managed addons are not Helm releases.** `aws-node`, `kube-proxy`, `coredns`,
   the EBS/EFS CSI drivers and the GuardDuty agent are installed by the EKS addon
   controller. They can only be labeled by patching, and a patch may be reverted on
   addon upgrade — untested.

Coverage that leaves `operations-production` at: 18 of 66 workloads carrying all three
of `service`, `component` and `alert_tier` (`alert_tier` was at zero), against 7
carrying `service` alone before. The remaining 48 are APISIX (2), the Grafana
`k8s-monitoring` subcharts (9), EKS-managed addons (9), NVIDIA (6), ToolHive (13),
Keycloak (2) and the 7 already-`service`-labeled application workloads, which need
`component` and `alert_tier` added at their own call sites.

Also backfill CI/QA clusters, which the analysis explicitly did not measure. Coverage
there does not affect paging (CI/QA alerts go Slack-only per §8.1) but an unlabeled QA
workload routes as untier-ed, so the QA stack stops being a rehearsal for the
production routing behaviour — which is how a Phase 3 mis-tiering would go unnoticed
until it reached production.

### 3.4 The CI gate

The obvious shape — walk every `K8sGlobalLabels(...)` / `K8sAppLabels(...)`
construction and assert the fields are set — does not enforce the invariant that
matters, for two reasons:

1. **It cannot see a workload that never constructs a label object at all.** A new
   `Deployment` with a hand-written `metadata.labels` dict, or one that reuses another
   module's `k8s_global_labels` variable, passes a constructor-walking check while
   producing an unlabeled workload. The failure mode the gate exists to catch is
   precisely "someone added a workload and didn't think about labels".
2. **It cannot tell where the labels landed.** `kube_pod_labels` reads the **pod
   template's** labels, not the workload object's. A `Deployment` labeled only at
   `metadata.labels` satisfies a constructor check and still joins against nothing.
   The analysis flagged this distinction in §3.1 and it is the difference between the
   gate working and the gate lying.

So the check must assert against **rendered workload resources**, not constructor call
sites: enumerate the `Deployment` / `StatefulSet` / `DaemonSet` resources each Pulumi
program registers, and assert the required keys are present in **both**
`metadata.labels` and `spec.template.metadata.labels`. `pulumi.runtime.set_mocks` gives
this without a cluster.

Gate on an explicit allowlist keyed to **resource names**, not call sites, that shrinks
as §3.3 lands: the check goes in green on day one and the allowlist is the backfill's
progress bar. Keying it to resources rather than call sites is also what makes the
allowlist a faithful progress bar — one call site can produce many workloads.

### 3.5 Acceptance criteria

- `kubectl` inventory of the three production clusters shows ≥ 95% of
  Deployments/StatefulSets/DaemonSets carrying `ol.mit.edu/service`,
  `ol.mit.edu/component` and `ol.mit.edu/alert_tier`.
- 100% for `operations-production`.
- The CI gate's allowlist is empty.
- `mypy` clean after Commit A, which is what proves the `| str` escape hatch is gone.

---

## 4. Phase 3 — wiring labels into alerting

**Coverage is a routing-quality gate, not an alert-availability gate.** Per §0.2, once
`metricLabelsAllowlist` is configured kube-state-metrics emits a `kube_pod_labels`
series for **every** pod with `Value: 1`, carrying the join keys whether or not that pod
has any of the allowlisted labels. So `group_left` succeeds for an unlabeled workload
and simply copies no `label_ol_mit_edu_*` column: the alert fires with an empty
`alert_tier` and falls through to §4.4's `severity` catch-all — warning → Low, critical
→ High, i.e. today's behaviour. It is **not** silenced.

That makes §3.5 a strong recommendation rather than a hard prerequisite: shipping
Phase 3 at partial coverage degrades gracefully to the status quo for the unlabeled
tail, workload by workload, and each backfilled label improves routing the moment it
lands. §5 sequences P3.B after P2.D for that reason rather than after all of P2.

**The one case that does silence is a missing right-hand series**, and §4.1's Dagster
drop rule is the only thing in this spec that creates one. It is scoped accordingly —
see the note there.

### 4.1 kube-state-metrics: both gates (§0.2, §0.3)

In `substructure/aws/eks/grafana.py`:

```python
# Gate 1 of 2: make kube-state-metrics attach our labels as label_* columns.
# The chart's default value here is NOT empty -- it carries a nodes=[...] entry
# that the Grafana Cloud Kubernetes integration's node views depend on, and Helm
# replaces lists rather than merging them. Carry it forward verbatim.
# Note the flat shape: telemetryServices["kube-state-metrics"]["metricLabelsAllowlist"].
# The schema has no nested "kube-state-metrics" key and does not set
# additionalProperties: false, so an extra level is silently ignored.
"telemetryServices": {
    "kube-state-metrics": {
        "deploy": True,
        "metricLabelsAllowlist": [
            _KSM_CHART_DEFAULT_NODE_LABELS,  # verbatim from chart 4.4.0 values.yaml
            f"pods=[{_OL_LABEL_KEYS}]",
            f"deployments=[{_OL_LABEL_KEYS}]",
            f"statefulsets=[{_OL_LABEL_KEYS}]",
            f"daemonsets=[{_OL_LABEL_KEYS}]",
            f"jobs=[{_OL_LABEL_KEYS}]",
        ],
    },
    ...
},

# Gate 2 of 2: the chart's default kube-state-metrics allow-list contains exactly
# one *_labels metric (kube_persistentvolumeclaim_labels), so without this Alloy
# drops every series above post-scrape and Mimir looks exactly as it does today.
"clusterMetrics": {
    "enabled": True,
    "collector": "alloy-metrics",
    "kube-state-metrics": {
        "metricsTuning": {
            "includeMetrics": [
                "kube_pod_labels",
                "kube_deployment_labels",
                "kube_statefulset_labels",
                "kube_daemonset_labels",
                "kube_job_labels",
            ],
        },
        # Dagster mints a pod name per run, so pod-level label series churn hard
        # in data-production (2,004 of the estate's 2,606 production pods).
        #
        # Scoped to run/step pods BY NAME rather than to the whole `dagster`
        # namespace. Dropping the namespace would also drop the long-lived
        # services in it -- the daemon, the webserver, the code-location
        # deployments -- and per section 4, a missing right-hand series is the
        # one case where group_left silences an alert outright rather than
        # leaving it untier-ed. Those services would lose PodOOMKilled* and
        # PodCrashLooping* entirely; excluding Dagster from KubernetesJobFailed*
        # in PromQL does not compensate, because that is a different rule.
        #
        # Verify the prefixes against live pod names before applying -- they are
        # set by the Dagster K8s run launcher and are not a stable API. If they
        # drift, the cost of getting this wrong is a re-added series, not a lost
        # alert, provided the regex stays anchored to run/step pods.
        "extraMetricProcessingRules": textwrap.dedent("""
            rule {
              source_labels = ["__name__", "namespace", "pod"]
              separator     = "@"
              regex         = "kube_pod_labels@dagster@dagster-(run|step)-.*"
              action        = "drop"
            }
        """),
    },
},
```

If the run-pod naming turns out not to be reliably matchable, drop this rule entirely
and pay for the ~2,000 series. Cost is the lesser risk here.

where `_OL_LABEL_KEYS = "ol.mit.edu/service,ol.mit.edu/component,ol.mit.edu/alert_tier,ol.mit.edu/environment"`.

**Verification, before touching any alert rule:**

```
count(kube_pod_labels)                                    → ~600  (2,606 minus dagster run pods)
count(kube_deployment_labels)                             → >0
count(kube_pod_labels{label_ol_mit_edu_alert_tier!=""})    → matches §3.5 coverage
count(kube_node_labels{label_topology_kubernetes_io_zone!=""})  → unchanged from today
count(kube_pod_labels{namespace="dagster"})               → >0, and covers the daemon,
                                                             webserver and code-location
                                                             pods but no dagster-run-* pod
```

The fourth is the regression check for the `nodes=[...]` trap in §0.3; the fifth is the
regression check for the drop rule's scoping — a zero there means the rule is matching
the whole namespace and would silence Dagster's long-lived services.

### 4.2 Rewrite the 16 joinable rules

Pattern, using `PodOOMKilledCritical` as the worked example. Gate clause first, per the
idiom already documented at `eks_general.py:108-116`:

```promql
label_replace(
  label_replace(
    sum by (cluster, namespace, pod, container,
            label_ol_mit_edu_alert_tier, label_ol_mit_edu_component) (
      (kube_pod_container_status_last_terminated_reason{cluster=~".*-(production)", reason="OOMKilled"} == 1)
      * on (cluster, namespace, pod) group_left(label_ol_mit_edu_alert_tier, label_ol_mit_edu_component)
        kube_pod_labels
      * on (cluster, namespace, pod, container) group_left()
        (increase(kube_pod_container_status_restarts_total[1h]) >= 3)
    ),
    "alert_tier", "$1", "label_ol_mit_edu_alert_tier", "(.*)"),
  "ol_component", "$1", "label_ol_mit_edu_component", "(.*)")
```

Per §1.7 the rule emits `alert_tier` and `ol_component`; `label_ol_mit_edu_*` never
leaves PromQL.

**Environment (§5.4 fix).** **All eight joinable pairs** select warning-vs-critical by
`cluster=~".*-(ci|qa)"` / `".*-(production)"` — `DaemonsetReplicasMissing*`, both
`Deployment*` pairs, `StatefulSetReplicasMissing*`, `PodCrashLooping*`,
`CeleryBeatPodRestarts*`, `PodOOMKilled*` and `KubernetesJobFailed*`. (`NodeNotReady*`
and `HPAAtMaxReplicas*` do too, but they are out of scope per §1.5.)

Adding a positive environment matcher alongside the cluster matcher is **not** a
fallback: `label_ol_mit_edu_environment="production"` excludes every workload that has
not declared one, so an unlabeled workload would drop out of the critical branch
entirely — the one place in this spec where a naive transition really does silence
alerts. Use a **negative** matcher instead, which in PromQL matches an absent label:

```promql
# critical branch: production clusters, minus anything that declares itself non-prod
kube_pod_labels{label_ol_mit_edu_environment!~"ci|qa|staging|rc"}

# warning branch: ci/qa clusters, plus anything in a prod cluster declaring non-prod
kube_pod_labels{label_ol_mit_edu_environment=~"ci|qa|staging|rc"}
```

Keep the `cluster=~` filter as the outer selector throughout. An unlabeled workload then
lands in its cluster's branch exactly as today, and a declared `staging` workload in
`residential-production` moves to the warning branch — which is the §5.4 fix. Removing
the cluster filter is a separate change once §3.5 holds, if ever.

**Two rule pairs stay as they are:** `HPAAtMaxReplicas*` (no joinable label, §0.5) and
`NodeNotReady*` (not a workload).

**Roll out one rule pair at a time**, verifying firing counts before and after. A
`group_left` whose right-hand series is missing produces silence, and silence is the
failure mode this whole project is meant to remove.

### 4.3 Alertmanager grouping

`alertmanager.py:123-138` groups on 14 labels including `application`, which the
analysis noted is only ever present on Loki-sourced alerts. Add `alert_tier` and
`ol_component` to `group_bies` once §4.2 emits them, and delete `application` if it is
still never set on a metric alert at that point. Coordinate with remediation spec W3b,
which adds a per-route grouping override for the OOM/crashloop pair — that override's
`group_bies` needs the same two labels or it will re-bundle across tiers.

### 4.4 Rootly: replace the allowlist with two urgency rules

On the **Production** source (`90cda8ea`, `__main__.py:3032-3076`), replace all 12
`alert_source_urgency_rules_attributes` entries with:

| Pos | Condition | → Urgency |
|---|---|---|
| 1 | `$.commonLabels.alert_tier is "ticket"` | Low |
| 2 | `$.commonLabels.alert_tier is "notify"` | Medium |
| 3 | `$.commonLabels.severity is "warning"` | Low |
| — | default | High |

Position 3 is retained deliberately: it catches every alert that carries no
`alert_tier` — Loki/log rules, `linux_host.py`, the `GrafanaCloud` folder's
`GrafanaMetricCount`, and any EKS rule not yet rewritten. Removing it would make an
untier-ed warning page.

`DiskUsageCritical` (position 2 today, an `alert_field` rule on Title rather than a
payload rule) also has no `alert_tier` — it is a `linux_host.py` rule about EC2
instances, not a K8s workload. Keep it, as a fourth rule.

**Do not add an environment-based urgency rule.** Per analysis §8.1, non-production is
a routing-destination concern (Slack-only escalation policy), not an urgency concern,
and the CI/QA sources are separate objects from the Production source anyway.

The existing `escalation_path_defer_medium_urgency_off_hours` (`:691`),
`escalation_path_defer_low_urgency_off_hours` (`:729`) and
`escalation_path_medium_urgency_slack_only` (`:758`) all continue to work unchanged,
and now defer and divert per-workload rather than per-alertname.

### 4.5 Rootly: component routing for the 19 orphaned services

Add rules to the **Grafana Production Service Route** (bound to `90cda8ea`) of the form
`namespace contains "<ns>"` AND `$.commonLabels.ol_component is "<component>"` →
`<Service>`. Every Celery service becomes reachable this way.

**Two of the 19 do not need any of that** — moving the existing `component` rules off
the now-inert Grafana Service Route reaches `MIT Learn - API` and `MIT Learn - NextJS`
immediately, because the Synthetic Monitoring rules already emit `service=mitlearn` and
`component=api|nextjs` to this source (§0.4). Sequenced separately as **P1.5** so it is
not blocked behind Phase 2.

Note the label-name mismatch this creates and resolve it deliberately: SM rules emit a
bare `component`, while §1.7 has the EKS rules emit `ol_component` to avoid colliding
with anything Grafana or a vendor integration might set. Either rename `_Check`'s label
to `ol_component` when P1.5 moves the rules, or accept two keys and write the routing
rules against both. Renaming is cleaner and is a one-line change in
`synthetic_monitoring.py`, but it retitles a live alert label — do it in the same change
as the rule move, not later.

Ordering caution, unchanged from analysis §4.3: conditions are `contains`, so the
narrow rules must precede the broad ones. Now that all 12 routes are in Pulumi
(#5218), rule position is reviewable in a diff — which is the first time this
hand-maintained invariant has been visible at all.

### 4.6 Acceptance criteria

- `count(kube_pod_labels{label_ol_mit_edu_alert_tier!=""})` ≥ 95% of non-dagster-run
  production pods.
- Firing counts per rule within ±20% of the pre-change 14-day baseline for the 16
  rewritten rules. A rule that goes to zero is a dropped right-hand series, not a
  fixed alert.
- `count(kube_pod_labels{namespace="dagster"}) > 0` — the drop rule is scoped to run
  pods and Dagster's long-lived services still join.
- Zero Rootly services with no routing rule targeting them, down from 19.
- The Production source carries four urgency rules, not twelve.
- `kube_node_labels` zone/instance-type columns unchanged (§0.3 regression check).
- A staging workload in `residential-production` receives `notify`, not `page`.
- An **unlabeled** production workload still alerts, at today's urgency — the check
  that coverage is a quality gate and not an availability gate (§4 preamble).

---

## 5. Sequence

| Step | Depends on | Effort |
|---|---|---|
| P1.1 Enable the two CI/QA route rules (UI) | — | minutes |
| P1.2 CI source urgency High → Low | — | small |
| P1.3 Turn off the account-wide default alerts channel | P1.1 | small |
| P1.4 Delete `exampleDeleteMe`, fix `QA Non-Paging`, finish `Cloudwatch - Critical` | — | small |
| **P1.5 Move the 2 live `component` rules to the Production Service Route; delete the inert Grafana Service Route** | — | **small — reaches 2 orphaned services today (§0.4)** |
| P2.A Extend `Component` + tighten the type; retype `_Check.component`; enum hygiene | — | small |
| P2.B Add `AlertTier`; move roll-up fields to base; explicit `environment` | P2.A | small |
| P2.C Backfill `operations-production` | P2.B | medium |
| P2.D Backfill `data-production`, `applications-production`, CI/QA | P2.B | **long pole** |
| P2.E CI gate against rendered workloads, shrinking allowlist | P2.B | small |
| P3.A Both kube-state-metrics gates + verification queries | P2.C | small |
| P3.B Rewrite 16 rules, one pair at a time | P3.A | medium |
| P3.C Alertmanager `group_bies` | P3.B | small |
| P3.D Rootly urgency rules: 12 → 4 | P3.B | small |
| P3.E Component routing for the remaining orphaned services | P3.B | medium |

P1.5 is independent of everything else here and is the cheapest item in the project —
do it first.

P3.A can start once `operations-production` is labeled (P2.C) — the gates are
cluster-wide and harmless with partial coverage. **P3.B does not need §3.5 met**: per
the §4 preamble, an unlabeled workload routes as untier-ed rather than going silent, so
the rewrite degrades to today's behaviour for the backfill's tail and improves
workload-by-workload as P2.D lands. Do P3.D last regardless — the urgency rules are what
turn a tier into a paging decision, and there is no reason to flip them before the tiers
they read are broadly populated.

---

## 6. Still open

1. **`alert_tier` default for unlabeled workloads.** §4.4 position 3 keeps
   `severity=warning → Low` as the catch-all, which means an unlabeled *critical* EKS
   alert defaults to High/page. That is the safe direction, but it means the backfill's
   remaining tail pages at full urgency. Alternative: default the tier to `notify` in
   `K8sGlobalLabels` rather than `None`. Rejected for now because a `None` tier is
   visible in the coverage query and a defaulted one is not — but revisit if P2.E
   stretches.
2. **`kube_job_labels` provenance.** Job labels are set by whatever launches the Job
   (Dagster, Celery beat, a CronJob spec), not by our Pulumi programs. Whether
   `KubernetesJobFailed*` can be tiered at all depends on those launchers propagating
   the labels; unverified. Treat that rule pair as best-effort until measured.
3. **QA/CI source default urgency.** Analysis §8.1's open sub-decision — move QA and CI
   defaults to Low for decoupling from the production Medium band. Remediation spec W2a
   covers CI; QA is still Medium. Low cost, do it with W2a.
4. **Cost of the new series.** ~600 pod-level plus ~262 workload-level active series
   per production cluster set, against a Grafana Cloud contract whose per-series cost
   is not recorded in this repo. Sized in §0.6 but not priced.
5. **`exec_err_state`** (remediation spec §0.11, open decision 5). Not this project's
   change, but it interacts: after §4.2, a rule whose *join* fails evaluates to no data,
   not to an error, so `no_data_state` governs — worth re-checking the `no_data_state`
   value on the 16 rewritten rules while editing them.
