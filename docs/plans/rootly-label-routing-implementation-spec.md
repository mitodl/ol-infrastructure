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
that overlap W0/W2 — but as *status and prerequisites*, deferring the implementation
detail to that spec. Where the two disagree about a Rootly object, that spec wins on
W0–W2 and this one wins on anything touching a label.

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
scraped. The `*_labels` metrics are absent entirely. That distinction is the finding
in §0.2.

### 0.2 [New] There are **two** independent gates, not one — and the analysis only found the first

kube-state-metrics emits `kube_pod_labels` / `kube_deployment_labels` /
`kube_statefulset_labels` unconditionally; `--metric-labels-allowlist` controls only
which `label_*` *columns* those series carry, not whether the series exist. Their
total absence from Mimir therefore cannot be explained by the missing allowlist.

The second gate is the chart's own scrape filter. In `k8s-monitoring` 4.4.0,
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
| `metricLabelsAllowlist` | `telemetryServices["kube-state-metrics"]` | series exist, carry no `label_ol_mit_edu_*` columns; `group_left` adds nothing |
| `metricsTuning.includeMetrics` | `clusterMetrics["kube-state-metrics"]` | series never reach Mimir; `group_left` drops every row |

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

### 0.4 Q4 resolved: the `Grafana` source is live. The three dead rules are dead for a different reason.

Analysis Q4 asked what still delivers to alert source `f4d836c0`
(`source_type: "grafana"`). Answer, from the production provisioning API:

```
contact point "Rootly"  uid eel3rjpiwahoge
  url = https://webhooks.rootly.com/webhooks/incoming/grafana_webhooks?secret=09f9d82f…
```

That secret is byte-identical to alert source `f4d836c0`'s `secret`. The source is
fed by the three Synthetic Monitoring rules that set
`notification_settings.receiver: "Rootly"` (remediation spec §0.7c) — the
UI-created duplicate contact point, distinct from Pulumi's `rootly`
(`bfsoqo63lsyrka`, the `alertmanager_webhooks` endpoint).

So the "Grafana Service Route" is **not** an orphan route to be deleted. But its five
rules are still unreachable, and the reason matters for Phase 3:

- Its only traffic is Synthetic Monitoring probe alerts, which carry neither
  `commonLabels.service` nor `commonLabels.component`.
- The EKS metric alerts that *will* carry a `component` label after Phase 3 arrive at
  the three `alertmanager`-type sources, never at `f4d836c0`.

**Therefore the three `component`-matching rules must be *moved* to the Grafana
Production Service Route (bound to source `90cda8ea`), not repaired in place.**
Repairing them where they sit — which is what analysis §8 step 3 implies — leaves
them dead forever. Correcting analysis §5.5, which speculated the route might be
entirely dead and removable: it is live, and removing it would break nothing today
but would remove the only route the SM alerts can match.

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

### 0.7 [New] Making `Component` strict breaks 3 of its 5 live call sites

`component` is set at exactly five places in `src/`, with these values:

```
"webapp"    ← in Component
"frontend"  ← in Component
"api"       ← NOT in Component
"nextjs"    ← NOT in Component
pgbouncer   ← NOT in Component (bare identifier, a local variable)
```

The `Component | str | None` type is what permits this. Dropping `| str` without
first extending the enum is a three-site breakage, and `pgbouncer` is a value nobody
proposed adding. This makes the ordering in §3.2 mandatory: extend the enum, migrate
call sites, *then* tighten the type — three commits, not one.

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
| 3 | Three dead `component` rules in the Grafana Service Route | this project | **re-specified** — *move* to the Grafana Production Service Route (§0.4), do not repair in place; sequence with Phase 3 step 13, since they only become live once `component` is emitted |
| 4 | Import the six unmanaged alert routes | — | **closed** by PR #5218 |
| 5 | Low urgency pages like High | — | **closed** by PRs #5354, #5377 |

**Prerequisite relation to Phase 3:** none of Phase 3 depends on Phase 1. Item 1c is a
prerequisite for *observing* whether any routing change worked, so it should land
before Phase 3's verification step, not before its implementation.

---

## 3. Phase 2 — schema and coverage

### 3.1 Schema changes, in dependency order

Three commits, because §0.7 makes the ordering load-bearing.

**Commit A — extend `Component`, keep the permissive type.**

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

`api`, `nextjs` and `pgbouncer` are present because they are already in use (§0.7).
`gateway` and `queue` are added for APISIX/Traefik and RabbitMQ/Redis-as-broker,
which have no current member and are exactly the wide-blast-radius workloads in
`operations-production`.

**Commit B — add `alert_tier`, and move the roll-up fields into the base class.**

```python
@unique
class AlertTier(StrEnum):
    """Paging eligibility for alerts about this workload."""

    page = "page"      # wake someone: user-facing or data-integrity impact
    notify = "notify"  # business-hours attention: degraded, self-healing, or redundant
    ticket = "ticket"  # record only, never notifies


class K8sGlobalLabels(BaseModel):
    ou: BusinessUnit
    service: Services
    stack: StackInfo
    # Moved down from K8sAppLabels: a base-class-labeled resource must still be
    # rollable-up to a product, and 26 of the 40 label call sites use this class.
    product: Product | None = None
    application: Application | None = None
    component: Component | str | None = None
    alert_tier: AlertTier | None = None
    # Overrides the value derived from stack.env_suffix in model_dump. A workload
    # whose logical environment differs from its cluster's -- mitx-staging-* in
    # residential-production -- must be able to say so; today it silently inherits
    # the cluster's answer and is treated as production. See analysis section 5.4.
    environment: Environment | str | None = None
```

`model_dump` changes one line: `ol.mit.edu/environment` becomes
`self.environment or self.stack.env_suffix`. Every existing call site is unaffected —
all four new fields default to `None` and `exclude_none=True` already drops them.

`K8sAppLabels` keeps `pod_security_group`, `source_repository`, `commit_sha`,
`release_tag` and re-declares `product`, `application` and `source_repository` as
required, so it stays the stricter contract it is today.

**Commit C — tighten the types, after the call sites are migrated.** `component:
Component | None`, dropping `| str`. Requires Commit A plus the `pgbouncer` call site
converted from a bare identifier to `Component.pgbouncer`.

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

Order by blast radius, not by count:

1. **`operations-production`** — 59 workloads, 57 unlabeled. APISIX, cert-manager and
   the shared ingress live here; these are the widest-blast-radius workloads in the
   estate and the worst-covered. Tier `page`, `component` = `gateway`/`ingress`.
2. **`data-production`** — 91 workloads, 87 unlabeled. Mostly Dagster and pipeline
   workloads; tier `ticket` or `notify`. High count, low paging stake.
3. **`applications-production`** — 61 of 112 unlabeled. Highest paging stake per
   workload; the 51 already labeled need `alert_tier` and `component` added, not
   labels from scratch.

Also backfill CI/QA clusters, which the analysis explicitly did not measure. Coverage
there does not affect paging (CI/QA alerts go Slack-only per §8.1) but an unlabeled QA
workload makes the join drop the series, so a rule that works in production silently
returns nothing in QA — which is how a Phase 3 regression would hide.

### 3.4 The CI gate

A `pytest` check (not a `pre-commit` hook — it needs to import the label classes) that
walks every `K8sGlobalLabels(...)` / `K8sAppLabels(...)` construction reachable from
`src/ol_infrastructure/applications/` and asserts `component` and `alert_tier` are
set. Fails the build on a new workload without them.

Gate on an explicit allowlist of currently-unlabeled construction sites that shrinks
as §3.3 lands, rather than a flag day: the check goes in green on day one and the
allowlist is the backfill's progress bar.

### 3.5 Acceptance criteria

- `kubectl` inventory of the three production clusters shows ≥ 95% of
  Deployments/StatefulSets/DaemonSets carrying `ol.mit.edu/service`,
  `ol.mit.edu/component` and `ol.mit.edu/alert_tier`.
- 100% for `operations-production`.
- The CI gate's allowlist is empty.
- `mypy` clean after Commit C, which is what proves the `| str` escape hatch is gone.

---

## 4. Phase 3 — wiring labels into alerting

**Hard prerequisite: §3.5 met.** The `group_left` join drops any series whose join
partner is missing, so an unlabeled workload does not degrade to "no tier" — it
degrades to **no alert at all**. Shipping Phase 3 at today's 22% coverage would
silence 78% of EKS alerting. This is the single largest risk in the project.

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
        # Dagster mints a pod name per job run, so pod-level label series churn
        # hard in data-production (2,004 of the estate's 2,606 production pods).
        # Those pods are tiered `ticket` and already excluded from
        # KubernetesJobFailed* in PromQL, so nothing is lost by not paying for them.
        "extraMetricProcessingRules": textwrap.dedent("""
            rule {
              source_labels = ["__name__", "namespace"]
              separator     = "@"
              regex         = "kube_pod_labels@dagster"
              action        = "drop"
            }
        """),
    },
},
```

where `_OL_LABEL_KEYS = "ol.mit.edu/service,ol.mit.edu/component,ol.mit.edu/alert_tier,ol.mit.edu/environment"`.

**Verification, before touching any alert rule:**

```
count(kube_pod_labels)                                    → ~600  (2,606 minus dagster)
count(kube_deployment_labels)                             → >0
count(kube_pod_labels{label_ol_mit_edu_alert_tier!=""})    → matches §3.5 coverage
count(kube_node_labels{label_topology_kubernetes_io_zone!=""})  → unchanged from today
```

That last one is the regression check for the `nodes=[...]` trap in §0.3.

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

**Environment (§5.4 fix).** For the four rule pairs that currently encode environment
by cluster-name regex, join `label_ol_mit_edu_environment` and filter on it instead of
on `cluster=~`. Keep the cluster filter as well during the transition — it is the
fallback for any workload that has not declared an environment, and removing it is a
separate change once §3.5 holds.

**Two rules stay as they are:** `HPAAtMaxReplicas*` (no joinable label, §0.5) and
`NodeNotReady*` (not a workload).

**Roll out one rule pair at a time**, verifying firing counts before and after. A
`group_left` that drops rows produces silence, and silence is the failure mode this
whole project is meant to remove.

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
`<Service>`. Every Celery service becomes reachable this way. Move the three dead
`component` rules here from the Grafana Service Route (§0.4) as part of the same change.

Ordering caution, unchanged from analysis §4.3: conditions are `contains`, so the
narrow rules must precede the broad ones. Now that all 12 routes are in Pulumi
(#5218), rule position is reviewable in a diff — which is the first time this
hand-maintained invariant has been visible at all.

### 4.6 Acceptance criteria

- `count(kube_pod_labels{label_ol_mit_edu_alert_tier!=""})` ≥ 95% of non-dagster
  production pods.
- Firing counts per rule within ±20% of the pre-change 14-day baseline for the 16
  rewritten rules. A rule that goes to zero is a dropped join, not a fixed alert.
- Zero Rootly services with no routing rule targeting them, down from 19.
- The Production source carries four urgency rules, not twelve.
- `kube_node_labels` zone/instance-type columns unchanged (§0.3 regression check).
- A staging workload in `residential-production` receives `notify`, not `page`.

---

## 5. Sequence

| Step | Depends on | Effort |
|---|---|---|
| P1.1 Enable the two CI/QA route rules (UI) | — | minutes |
| P1.2 CI source urgency High → Low | — | small |
| P1.3 Turn off the account-wide default alerts channel | P1.1 | small |
| P1.4 Delete `exampleDeleteMe`, fix `QA Non-Paging`, finish `Cloudwatch - Critical` | — | small |
| P2.A Extend `Component`; enum hygiene | — | small |
| P2.B Add `AlertTier`; move roll-up fields to base; explicit `environment` | P2.A | small |
| P2.C Tighten `component` type | P2.B + call-site migration | small |
| P2.D Backfill `operations-production` | P2.B | medium |
| P2.E Backfill `data-production`, `applications-production`, CI/QA | P2.B | **long pole** |
| P2.F CI gate with shrinking allowlist | P2.B | small |
| P3.A Both kube-state-metrics gates + verification queries | P2.D | small |
| P3.B Rewrite 16 rules, one pair at a time | P3.A + §3.5 | medium |
| P3.C Alertmanager `group_bies` | P3.B | small |
| P3.D Rootly urgency rules: 12 → 4 | P3.B | small |
| P3.E Component routing for the 19 orphaned services; move the 3 dead rules | P3.B | medium |

P3.A can start once `operations-production` is labeled (P2.D) — the gates are
cluster-wide and harmless with partial coverage. P3.B cannot start until §3.5 holds.

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
