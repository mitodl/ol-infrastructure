# Labeling Hierarchy and Rootly Alert Routing — Gap Analysis

**Date:** 2026-07-30
**Author:** Analysis session (tmacey)
**Scope:** `ol.mit.edu/*` Kubernetes label hierarchy, AWS resource tags, Grafana Cloud
alert rules and notification policy, Rootly alert sources / routes / urgencies /
escalation, non-Grafana alert sources (CloudWatch, Sentry, Pingdom, email), and
Heroku/EC2 log labels.

---

## 0. Method and evidence base

What was inspected:

| Source | How |
|---|---|
| Label schema | `src/ol_infrastructure/lib/ol_types.py` |
| Label application | Live `kubectl` inventory of 262 Deployments/StatefulSets/DaemonSets across `applications-production`, `data-production`, `operations-production` |
| Alert rules | `src/ol_infrastructure/infrastructure/grafana_alerting/` (metric_rules, log_rules, pingdom_checks, alertmanager) |
| Metric label availability | Live PromQL against the production Mimir tenant (`grafanacloud-prom`) |
| Rootly config | Live Rootly API: 9 alert sources, 12 alert routes (~70 rules), 3 urgencies, 4 escalation policies, 41 services |
| Rootly as code | `src/ol_infrastructure/saas/rootly/__main__.py` (3,253 lines) + `KNOWN_ISSUES.md` |
| Alert volume | Live Rootly alerts API, 1,070 alerts over 2026-07-16 → 2026-07-30; two 20-alert pages sampled in detail |

**Not inspected:** CI/QA cluster label coverage (production only was measured);
the full 1,070-alert corpus (40 sampled); Grafana CI/QA stack rule definitions
(assumed identical to Production stack since all three deploy from the same
Pulumi program).

---

## 1. Headline finding

**The `ol.mit.edu/*` label hierarchy is not used by Rootly at all.** Not one of
the ~70 routing rules across the 12 live alert routes references an `ol.mit.edu`
label, and no alert that reaches Rootly carries one.

This is not a configuration oversight that can be fixed in Rootly. The labels are
structurally unable to reach Rootly on the metric path, for two independent
reasons:

1. **kube-state-metrics does not export them.** `src/ol_infrastructure/substructure/aws/eks/grafana.py:283`
   deploys kube-state-metrics via the `k8s-monitoring` chart with
   `{"kube-state-metrics": {"deploy": True}}` and no `metricLabelsAllowlist`. Verified
   live: `kube_pod_labels`, `kube_deployment_labels`, and `kube_namespace_labels`
   return **zero series** in the production Mimir tenant, and a label-names query
   scoped to `kube_pod_labels` returns `[]`. The metrics carrying workload identity
   simply do not exist.

2. **Every EKS alert rule aggregates the labels away.** Every rule in
   `metric_rules/eks_general.py` is of the form
   `sum by (cluster, namespace, pod, container) (...)`. Even if
   `label_ol_mit_edu_service` existed on the source series, `sum by` would discard
   it before the alert fires.

The consequence: the only identity an EKS alert carries into Rootly is
`cluster`, `namespace`, and a resource name. Everything the label hierarchy is
designed to express — owning business unit, product, application, component
(celery vs webapp vs cache) — is absent at the moment routing decisions are made.

---

## 2. There are four parallel label vocabularies, not one

The "hierarchy" the team designed is one of four coexisting vocabularies. They
overlap in key names but not in values, and nothing validates them against each
other.

| # | Vocabulary | Keys | Where set | Reaches Rootly? |
|---|---|---|---|---|
| 1 | **`ol.mit.edu/*` K8s labels** | `ou`, `service`, `stack`, `environment`, `product`, `application`, `component`, `source_repository`, `commit_sha`, `release_tag`, `pod_security_group`, `slack-channel` | `K8sGlobalLabels` / `K8sAppLabels`, `ol_types.py:170` | **No** |
| 2 | **Prometheus/Mimir metric labels** | `cluster`, `namespace`, `pod`, `container`, `deployment`, `statefulset`, `daemonset`, `horizontalpodautoscaler`, `node`, `job_name` | kube-state-metrics defaults | **Yes** — this is what routing actually uses |
| 3 | **Loki stream labels (EC2/Heroku)** | `application`, `environment`, `service` | Vector: `src/bilder/components/vector/templates/global_log_sink.yaml:16-18`, defaulting to `missing_environment` / `missing_application` / `missing_service` | **Yes**, for log-based rules only |
| 4 | **Pingdom check tags** | `env`, `service` | `pingdom_checks.py` | Into Pingdom, not into the Rootly payload |

Vocabulary 3 and 4 use *different values for the same concepts* than vocabulary 1:

| Concept | Vocab 1 (`Services` enum) | Vocab 3 (Loki) | Vocab 4 (Pingdom) |
|---|---|---|---|
| MIT Learn | `mit-learn` | — | `learn` |
| Keycloak | `keycloak` | `keycloak` | `sso` |
| Open edX residential | `mitx-edx` | `edxapp` | `mitx` |
| OCW | `ocw-build` / `ocw-studio` | `ocw-studio` | `ocw`, `ocw-studio` |
| Environment key | `environment` | `environment` | `env` |

Vocabulary 4 also uses environment values (`qa`, `rc`, `production`, `staging`)
that are not in the `Environment` enum, and service values
(`open-discussions`, `concourse`, `micromasters`) that partly are and partly
aren't in `Services`.

---

## 3. Consistency of the `ol.mit.edu/*` labels themselves

### 3.1 Measured coverage (production clusters, live)

| Cluster | Workloads | `service` | `ou` | `application` | `component` | `product` | `environment` | Fully unlabeled |
|---|---|---|---|---|---|---|---|---|
| `applications-production` | 112 | 51 (46%) | 51 | 47 | 45 | 34 | 51 | 61 (54%) |
| `data-production` | 91 | 4 (4%) | 4 | 3 | 1 | 2 | 4 | 87 (96%) |
| `operations-production` | 59 | 2 (3%) | 2 | 0 | 0 | 0 | 2 | 57 (97%) |
| **Total** | **262** | **57 (22%)** | **57** | **50** | **46** | **36** | **57** | **205 (78%)** |

**78% of production workloads carry no `ol.mit.edu` label at all.** Coverage is
concentrated almost entirely in `applications-production`; the data and operations
clusters are effectively unlabeled. `operations-production` is where APISIX,
cert-manager, and the shared ingress live — the components whose failures have the
widest blast radius — and it has the worst coverage.

Note this measures the *workload* object's labels, not pod template labels; a
workload labeled only at the pod level would still not help, since routing never
sees pod labels either (see §1).

### 3.2 Defects in the schema

**a. `K8sGlobalLabels` cannot roll up to a product.** The base class carries only
`ou`, `service`, `stack` (`ol_types.py:170-177`). `product`, `application`, and
`component` exist only on the `K8sAppLabels` subclass. A resource labeled with the
base class — which is the common case; the variable is literally named
`k8s_global_labels` in most `__main__.py` files — cannot be rolled up to a product.
This is visible in the survey: 57 workloads have `service` but only 36 have
`product`.

**b. `Component` is a four-value enum that isn't enforced.**
`Component` (`ol_types.py:144`) has exactly four members: `celery`, `webapp`,
`frontend`, `keycloak`. But the field is typed `Component | str | None`
(`ol_types.py:210`), so any string is accepted and `None` is the default. There is
no `worker`, `beat`, `api`, `nextjs`, `cache`, `database`, or `search` member —
precisely the distinctions needed to tell a Celery worker apart from a webapp for
paging purposes.

**c. `Services` and `Application` are near-duplicate enums that have drifted.**
Both enumerate ~36 near-identical members. Divergences:

- In `Services` but not `Application`: `notebooks`, `ol-analytics-api`, `open-edx`, `openedx`, `opik`
- In `Application` but not `Services`: `mit-learn-nextjs`, `openedx-platform`
- `Services` contains both `open-edx` and `openedx` as distinct members

Nothing constrains `service` and `application` to be consistent on the same
resource.

**d. `Services.learn_ai` is declared as a tuple.** `ol_types.py:79` reads
`learn_ai = ("learn-ai",)`. This happens to be harmless — `StrEnum` splats the
tuple into `str()`, so the value is correctly `'learn-ai'` (verified) — but it is
inconsistent with every other member and would break under a different enum base.

**e. `BusinessUnit` mixes organizational units with environments.** It contains
both `residential` and `residential_staging`. Staging is an environment, not a
business unit, so cost-allocation and ownership roll-ups split a single OU in two.

**f. `environment` is derived, not declared.** `model_dump` sets
`ol.mit.edu/environment` from `self.stack.env_suffix` (`ol_types.py:203`), i.e. from
the Pulumi stack name. A workload deployed into a cluster whose environment differs
from its own logical environment gets the cluster's answer. This is not theoretical —
see §5.4.

### 3.3 AWS tags are a fifth, disconnected vocabulary

`AWSBase` (`ol_types.py:217`) enforces `REQUIRED_TAGS = {"OU", "Environment"}` and
recommends `{"Application", "Owner"}`, validating `OU` against `BusinessUnit`. These
tags are real and consistently applied, but they **cannot** reach Rootly: CloudWatch
alarm notifications delivered via SNS do not include the tags of the resource being
alarmed on. The CloudWatch→SNS→Rootly path therefore routes on **substring matching
of the alarm name** instead (§4.3). The AWS tag hierarchy is a cost-allocation
mechanism only; it is not, and without a transformation step cannot be, an alert
routing mechanism.

---

## 4. What Rootly actually routes on

### 4.1 Pipeline

```
EKS workloads ──► kube-state-metrics (no label allowlist) ──► Alloy ──► Mimir
                                                                         │
EC2/Heroku ──► Vector (application/environment/service) ──► Loki ────────┤
                                                                         ▼
                                          Grafana-managed alert rules (per-stack)
                                                          │  labels: severity [+ service, channel, environment]
                                                          ▼
                                          Grafana Alertmanager notification policy
                                            severity=warning|critical ──► Rootly webhook
                                            alertname=~Kube.*         ──► oblivion (dropped)
                                            channel=notifications-ocw-misc ──► Slack
                                                          ▼
                        Rootly alert source (bearer token selects CI / QA / Production)
                                                          │
                                     ┌────────────────────┴────────────────────┐
                                     ▼                                          ▼
                       alert_source_urgency_rules                        alert routes
                       (assigns High/Medium/Low)                (assign Service or Escalation Policy)
                                     └────────────────────┬────────────────────┘
                                                          ▼
                                             Default Escalation Policy
                                              + "Defer Medium urgency
                                                 outside business hours" path
```

### 4.2 The complete inventory of routing keys in use

Every condition across all 12 live alert routes, by JSON path:

| JSON path | Routes using it | What it is |
|---|---|---|
| `$.commonLabels.namespace` | Grafana Production Service Route (4 rules) | K8s namespace |
| `$.commonLabels.service` | Grafana Production Service Route (3), Grafana Service Route (4) | Rule-level label, set on only 3 alert rules |
| `$.commonLabels.component` | Grafana Service Route (3 rules) | **Never set by any alert rule** |
| `$.commonLabels.environment` | Grafana Production Service Route (2 rules) | Loki stream label |
| `$.Message.AlarmName` | Cloudwatch Service Route (10 rules) | CloudWatch alarm name substring |
| `$.check_name` | Pingdom Service Route (23 rules) | Pingdom check name substring |
| `$.data.metric_alert.projects[0]` | Sentry Service Route (2 rules) | Sentry project name |

Nothing here is `ol.mit.edu/*`. Nothing here is an AWS tag.

### 4.3 Routing is substring matching on names, not label matching

Every condition uses `property_field_condition_type: "contains"`. This creates
ordering-dependent behaviour that is easy to get wrong:

- Cloudwatch Service Route position 4 is `AlarmName contains "mitlearn"` → MIT Learn
  Django Webapp. Positions 2 and 3 (`mitlearn`+`rds`, `mitlearn`+`elasticache`) must
  precede it or all MIT Learn database alarms would land on the webapp service. They
  currently do — but this is a hand-maintained invariant with no test.
- Pingdom Service Route position 17 is `check_name contains "xPro"`, positions 15–16
  are `xPro CMS` / `xPro LMS`. Same fragility.
- Cloudwatch environment demotion is `AlarmName contains "-qa-"` / `"-ci-"`
  (§4.4). Any alarm whose name doesn't happen to embed a hyphenated environment
  token is treated as production.

### 4.4 Urgency assignment

Three urgencies exist: **High**, **Medium**, **Low**. Assignment is entirely at the
**alert source** level, via `alert_source_urgency_rules`, evaluated in order:

**Grafana Prometheus – Production** (`90cda8ea`), default **High**:

| Pos | Condition | → Urgency |
|---|---|---|
| 1 | `$.commonLabels.severity is "warning"` | Low |
| 2 | Title is `DiskUsageCritical` | Medium |
| 3–12 | `$.commonLabels.alertname is` one of: `PodCrashLoopingCritical`, `PodOOMKilledCritical`, `CeleryBeatPodRestartsCritical`, `DaemonsetReplicasMissingCritical`, `StatefulSetReplicasMissingCritical`, `KubernetesJobFailedCritical`, `CertManagerACMEIssuerUnavailableProduction`, `CertManagerChallengePresentationFailureProduction`, `OCWStudioContentSyncInvalidPasswordProd`, `HPAAtMaxReplicasCritical` | Medium |

Everything else → **High** (pages immediately).

**Grafana Prometheus – QA** (`074e2b0e`): default **Medium**, **no urgency rules**.
**Grafana Prometheus – CI** (`6a32d158`): default **High**, **no urgency rules**.
**Cloudwatch Critical / Warning**: default High; `AlarmName contains "-qa-"` or `"-ci-"` → Medium.
**Sentry**: default Low; `$.data.action is_not "critical"` → Low.
**Pingdom**: default High; `$.importance_level is "LOW"` → Low.

### 4.5 Escalation and the deferral path

Four escalation policies exist:

| Policy | ID | Services attached |
|---|---|---|
| Default Escalation Policy | `96629210` | 37 |
| QA Non-Paging Escalation Policy | `d63b7456` | 1 |
| CI/QA Slack Notifications | `b32c5938` | **0** |
| exampleDeleteMe-EscalationPolicy | `61e8291e` | 0 |

The Default Escalation Policy carries the noise control that currently exists: an
`EscalationPath` named **"Defer Medium urgency outside business hours"**
(`rootly/__main__.py:610`), `path_type="deferral"`, matching
`alert_urgency in [Medium]` AND a deferral window covering weeknights 17:00–09:00
and all weekend, with `after_deferral_behavior="re_evaluate"`.

**This is a well-built mechanism and it works.** Medium-urgency alerts arriving
overnight or on weekends are held and replayed at 09:00 ET rather than paging. The
problem is not the mechanism; it is the granularity of what feeds it (§5.1).

---

## 5. Gap inventory

Ordered by operational impact on on-call.

### 5.1 [High] Noise suppression is per-alert-rule, so it cannot distinguish workloads

The demotion list in §4.4 operates on `alertname`. `PodOOMKilledCritical` is demoted
to Medium **for every workload in every production cluster**. That is correct for
`mitlearn-embeddings-celery-worker` and wrong for a Postgres StatefulSet, an APISIX
ingress controller, or an OpenSearch data node — all of which would also be demoted
and deferred to the next business morning.

The same applies to `HPAAtMaxReplicasCritical`, `StatefulSetReplicasMissingCritical`,
and `KubernetesJobFailedCritical`.

The `ol.mit.edu/component` label is exactly the discriminator needed here and it is
not available at routing time.

**Live evidence.** In the last 14 days Rootly ingested **1,070 alerts** (~76/day).
In a 40-alert sample across two pages:

| Alert | Count | Share |
|---|---|---|
| `PodOOMKilled{Warning,Critical}` | 23 | 57.5% |
| `StatefulSetReplicasMissing*` | 7 | 17.5% |
| `PodCrashLoopingWarning` | 5 | 12.5% |
| `HPAAtMaxReplicasCritical` | 4 | 10% |
| `DeploymentUnavailableWarning` | 1 | 2.5% |

`mitlearn-default-celery-worker` in `applications-qa` OOM-killed and re-alerted on a
roughly hourly cadence through 2026-07-24 — 10 separate Rootly alerts in that one
sample day, each a distinct alert record.

### 5.2 [High] 78% of CI/QA alert volume is unrouted, at a higher urgency than production warnings

In the same 40-alert sample, **31 of 40 (78%) originated from CI or QA clusters**
(`applications-qa`, `data-qa`, `residential-qa`).

These alerts:

- Arrive at the QA source, whose **default urgency is Medium** with no urgency rules.
- Match the "Grafana Prometheus QA - Slack Warnings Route", whose **only rule is a
  disabled fallback rule** (`enabled: false`). No route condition matches them.
- The route's fallback target is the "CI/QA Slack Notifications" escalation policy
  (`b32c5938`), which is **correctly built**: one level, position 1, delay 0,
  notification target a Slack channel (`C0BK6BHUCDP`) with **no schedule target** —
  i.e. a genuine notify-don't-page destination.

Net effect: a QA `StatefulSetReplicasMissingWarning` is **Medium** urgency, while a
**production** `severity=warning` alert is demoted to **Low** by the production
source's position-1 rule. Non-production alerts outrank production alerts.

The CI source is worse: its default urgency is **High** with no demotion rules at all.

**The machinery for the correct behaviour already exists and is switched off.** The
two routes and the Slack-only escalation policy were all created on 2026-07-22. The
only thing standing between today's state and correct QA handling is that the two
fallback rules have `enabled: false`. Because a fallback rule targets the escalation
policy *directly* (`target_type: "EscalationPolicy"`), the policy's empty
`service_ids` list is irrelevant to this path — no service attachment is needed.

Separately broken: the **"QA Non-Paging Escalation Policy"** (`d63b7456`) has one
service attached (`MITx Online QA - Open edX - Redis`, created 2026-05-28 for MITx
Online QA CloudWatch alarms) but **zero escalation levels**. Alerts routed to that
service notify nobody at all. That is silent loss, not deferral.

Related: `setup_grafana()` returns early for CI (`grafana.py:~90`,
`if stack_info.env_suffix.lower() == "ci": return`), so CI clusters ship no metrics.
But every warning-severity rule in `eks_general.py` filters
`cluster=~".*-(ci|qa)"` — the `ci` half of that regex can never match anything.

### 5.3 [High] Half the Rootly service catalog is unreachable

Rootly has **41 services**, modelled at component granularity — `MIT Learn - Celery`,
`MITx Online - Open edX - LMS - Celery`, `MIT Learn - Redis`, `MIT Learn - Qdrant`,
`ODL Video - Celery`, and so on. This catalog *is* the roll-up hierarchy the labels
were designed to express.

But routing resolves at **namespace** granularity. Every alert from the `mitlearn`
namespace — webapp, celery worker, celery beat, nginx — routes to
`MIT Learn - Django - Webapp`.

**19 of 41 services are never the destination of any routing rule:**

`MIT Learn - Celery`, `MIT Learn AI - Celery`, `MIT Learn AI - Postgres`,
`MIT Learn AI - Redis`, `MIT Learn - Tika`, `MIT Learn - Qdrant`,
`MIT Learn - OpenSearch`, `MIT Learn - Keycloak - Postgres`,
`MIT Learn - Keycloak - Webapp`, `MITx Online - Open edX - CMS - Celery`,
`MITx Online - Open edX - LMS - Celery`, `MITx Online - Open edX - MongoDB`,
`MITx Online - Open edX - MySQL`, `MITx Online - Open edX - OpenSearch`,
`ODL Video - Celery`, `ODL Video - Postgres`, `CatchAll`, `API - Authentication`,
`UI - User Profile Block`.

This is the single clearest demonstration that the labeling hierarchy and the
routing configuration were designed against each other but never connected. The
Celery services exist. The alerts about Celery workers exist. Nothing joins them.

### 5.4 [Medium] Environment is inferred from cluster name, and it is already wrong

`eks_general.py` encodes severity by cluster-name regex: `.*-(ci|qa)` → warning,
`.*-(production)` → critical. Observed in the live alert stream:

> `StatefulSetReplicasMissingCritical` — statefulset `mitx-staging-ts-sts` in
> namespace `mitx-staging-openedx` in cluster `residential-production`

A **staging** workload received **Critical** severity, and therefore production
urgency treatment, because it runs in a cluster whose name ends in `-production`.
`ol.mit.edu/environment` on that workload would say `staging`.

### 5.5 [Medium] Three Rootly routing rules can never match

The "Grafana Service Route" contains three rules conditioned on
`$.commonLabels.component` (`= "api"`, `= "nextjs"`). An exhaustive enumeration of
every `labels={...}` in `grafana_alerting/` produces only these keys: `severity`,
`environment`, `channel`, `service`, `env`. **`component` is never set.** The rules
targeting `MIT Learn AI - API`, `MIT Learn - API`, and `MIT Learn - NextJS` are dead.

Additionally, the "Grafana Service Route" is bound to alert source `f4d836c0`
(`source_type: "grafana"`), while the Pulumi-managed contact point posts to
`.../alertmanager_webhooks` — an `alertmanager`-type source. It is unclear what, if
anything, still delivers to `f4d836c0`. See open question Q4.

### 5.6 [Medium] The routes carrying all the real rules are not managed as code

`rootly/__main__.py` declares **6** `AlertRoute` resources:
Grafana Prometheus CI Slack Warnings, Grafana Prometheus QA Slack Warnings,
Cloudwatch Catch-All, Grafana Production Catch-All, Pingdom Catch-All, Platform
Engineering Team Email Monitor.

All six are catch-all or fallback routes. The **six routes that contain every
service-mapping rule** — Cloudwatch Service Route (11 rules), Sentry Service Route
(3), Grafana Production Service Route (8), Pingdom Service Route (24), Grafana
Service Route (5), and `sh-test` (0) — are **not in Pulumi**. Roughly 51 routing
rules, the entire operational routing surface, are UI-managed with no review, no
history, and no drift detection.

`KNOWN_ISSUES.md` states alert routes were imported "excluding the empty-state Chris
Test route", which understates the gap.

### 5.7 [Low] Stale and misconfigured Rootly objects

- `sh-test` alert route: bound to the CI source, **zero rules**.
- `exampleDeleteMe-EscalationPolicy`: still present, in Pulumi (`__main__.py:492`).
- `Cloudwatch - Critical` alert source: status `setup_incomplete`. The *Critical*
  CloudWatch path is not fully connected.
- `Cloudwatch Catch-All Route` and both CI/QA Slack Warnings routes: fallback rules
  `enabled: false`.
- `CI/QA Slack Notifications` escalation policy: zero services.
- The `noise` field on all 1,070 sampled alerts is `not_noise` — Rootly's noise
  classification is unused.

### 5.8 [Low] Alert grouping was widened without the labels it was widened for

`alertmanager.py:123-138` sets `group_bies` to 14 labels including `application`,
after a 2026-07-23 incident where unrelated HPA alerts kept re-bundling. The
comment is accurate about the fix. But `application` is only present on
Loki-sourced (EC2/Heroku) alerts, never on EKS metric alerts, and the list contains
no `service`, `component`, `product`, or `ou` — because those don't exist on alerts
either. The grouping is as fine-grained as the available labels allow, which is the
same ceiling as the routing.

---

## 6. The OOMKilled / HPA question — analysis within the current model

*(As requested: gaps only in this section; the proposed scheme is §7, kept separate.)*

The team has already correctly identified this problem and built a real mechanism for
it. The current design is:

1. Rule-level tuning in PromQL — `PodOOMKilledCritical` requires `>= 3` restarts in
   an hour; `HPAAtMaxReplicasCritical` excludes HPAs where `min == max`. Both carry
   detailed comments explaining the reasoning and the production observations behind
   them (`eks_general.py:232-268`, `332-341`). This is good work and it demonstrably
   removed a class of false positives.
2. Alertname-based demotion to Medium in Rootly.
3. Business-hours deferral of Medium urgency.

The gaps that remain, all traceable to missing labels:

**G1 — Demotion is all-or-nothing per rule.** A Celery worker OOM and a Postgres
StatefulSet OOM are indistinguishable at routing time, so both are demoted. The
current setting optimises for the common case (Celery) and accepts the risk on the
uncommon one (stateful data services).

**G2 — There is no way to express "this workload is expected to OOM."** MIT Learn's
VPA-driven workloads intentionally start at a low memory floor (documented at
`eks_general.py:240-248`). That intent lives in a code comment, not in a label the
alerting pipeline can read.

**G3 — The `Component` enum lacks the values the decision needs.** `celery` exists,
but `worker` vs `beat` do not, and there is no `cache`/`database`/`search` for the
stateful services that should *not* be demoted.

**G4 — QA noise is the larger problem and is untouched by any of this.** 78% of alert
volume is CI/QA, and none of it is affected by the demotion list, because the
demotion rules are configured only on the Production source. Fixing per-workload
demotion would improve the 22%; fixing QA routing would improve the 78%.

**G5 — The demotion list requires a Rootly UI edit per new alert rule.** It is a
hand-maintained allowlist of 11 alertnames, living in Pulumi
(`rootly/__main__.py`, alert source urgency rules) but conceptually coupled to
`eks_general.py`. Adding a rule in one place silently defaults to High in the other.

---

## 7. Proposed scheme — paging eligibility as a label

*(Presented separately, per the brief. This is an option with a real migration cost,
not a recommendation to adopt as-is.)*

### 7.1 Principle

Move the page/notify decision from *"which rule fired"* to *"which workload fired
it, and how critical is that workload"*. Encode criticality on the workload, where
the team that owns it already edits the manifest, and carry it through to Rootly.

### 7.2 Schema change

Add to `K8sAppLabels` in `ol_types.py`:

```python
@unique
class AlertTier(StrEnum):
    """Paging eligibility for alerts about this workload."""

    page = "page"  # Wake someone up. User-facing or data-integrity impact.
    notify = "notify"  # Business-hours attention. Degraded, self-healing, or redundant.
    ticket = "ticket"  # Record only. Never notifies.
```

and extend `Component` with the values the decision actually needs:

```python
@unique
class Component(StrEnum):
    celery = "celery"  # existing
    webapp = "webapp"  # existing
    frontend = "frontend"  # existing
    keycloak = "keycloak"  # existing
    worker = "worker"
    beat = "beat"
    api = "api"
    nextjs = "nextjs"
    cache = "cache"
    database = "database"
    search = "search"
    ingress = "ingress"
```

Also: **move `product`, `application`, and `component` from `K8sAppLabels` down into
`K8sGlobalLabels`** (making `product`/`application` optional there), so that
base-class-labeled resources are still rollable-up. Without this, adding `alert_tier`
to the subclass reproduces the existing 46%-vs-32% coverage gap.

### 7.3 Making the labels visible to alerts

This is the load-bearing change and the one with real cost. Three options:

**Option A — kube-state-metrics label allowlist (recommended).** In
`substructure/aws/eks/grafana.py`, set:

```python
"telemetryServices": {
    "kube-state-metrics": {
        "deploy": True,
        "kube-state-metrics": {
            "metricLabelsAllowlist": [
                "pods=[ol.mit.edu/service,ol.mit.edu/component,ol.mit.edu/alert-tier,ol.mit.edu/environment]",
                "deployments=[ol.mit.edu/service,ol.mit.edu/component,ol.mit.edu/alert-tier,ol.mit.edu/environment]",
                "statefulsets=[ol.mit.edu/service,ol.mit.edu/component,ol.mit.edu/alert-tier,ol.mit.edu/environment]",
            ],
        },
    },
    ...
}
```

This makes `kube_pod_labels{label_ol_mit_edu_alert_tier="notify", ...}` available.
Alert rules then join against it:

```promql
sum by (cluster, namespace, pod, container, label_ol_mit_edu_alert_tier, label_ol_mit_edu_component) (
  (kube_pod_container_status_last_terminated_reason{cluster=~".*-(production)", reason="OOMKilled"} == 1)
  * on (cluster, namespace, pod) group_left(label_ol_mit_edu_alert_tier, label_ol_mit_edu_component)
    kube_pod_labels
  * on (cluster, namespace, pod, container) group_left()
    (increase(kube_pod_container_status_restarts_total[1h]) >= 3)
)
```

Cost: one new metric series per pod (cardinality is bounded and modest at this
fleet size); a `group_left` join added to each of the 18 EKS rules; the four
allowlisted labels must actually be present or the join drops the series — which
makes §3.1's 78% unlabeled figure a hard blocker, not a nice-to-have.

**Option B — Alertmanager-side enrichment.** Leave the rules alone; map
`namespace` → tier in the notification policy. Cheap, but reproduces the
namespace-granularity ceiling and cannot distinguish celery from webapp.

**Option C — Rootly-side catalog lookup.** Use Rootly's catalog entities to map
`namespace`+`pod` regex → service + urgency. Keeps the change out of the metrics
pipeline entirely, but moves the mapping into a system the infrastructure team
doesn't manage in code, worsening §5.6.

Recommendation: **A**, gated on closing the label coverage gap first.

### 7.4 Resulting Rootly configuration

Replace the 11-entry alertname demotion list on the **Production** source with two
source-level urgency rules:

| Pos | Condition | → Urgency |
|---|---|---|
| 1 | `$.commonLabels.label_ol_mit_edu_alert_tier is "ticket"` | Low |
| 2 | `$.commonLabels.label_ol_mit_edu_alert_tier is "notify"` | Medium |
| — | default | High |

The existing "Defer Medium urgency outside business hours" path continues to work
unchanged, and now defers the right things.

Note there is deliberately **no environment-based rule here**. Per §8.1, CI/QA
handling is a *routing destination* concern (Slack-only escalation policy), not an
*urgency* concern, and the CI/QA sources are separate from the Production source
anyway. Mixing the two would couple QA behaviour to the production Medium band.

Then add a routing rule per component to reach the 19 orphaned services, e.g.
`namespace contains "mitlearn"` AND `label_ol_mit_edu_component is "celery"` →
`MIT Learn - Celery`.

### 7.5 Which workloads would be reclassified

Based on the current production inventory and observed alert patterns:

| Workload class | Tier | Effect vs today |
|---|---|---|
| `*-celery-worker`, `*-celery-beat` (mitlearn, mitxonline, learn-ai, edxapp) | `notify` | Same as today (already demoted) — but now demoted *because they are workers*, not because of the alert name |
| `mitlearn-embeddings-celery-worker` | `notify` | Same |
| `apache-apisix` / ingress controllers | `page` | **Escalated** — currently demoted for OOM/crashloop |
| Postgres / MySQL / MongoDB StatefulSets | `page` | **Escalated** — currently demoted for `StatefulSetReplicasMissingCritical` |
| OpenSearch / Qdrant / ClickHouse / StarRocks | `page` | **Escalated** |
| `*-app` webapps (mitlearn, mitxonline, xpro) | `page` | Escalated for OOM; unchanged for unavailability |
| `mitx-staging-*` in `residential-production` | `notify` | **Demoted** — fixes §5.4 |
| Anything in a CI/QA cluster | n/a — handled by route destination, not tier (§8.1) | Slack-only, no page, signal preserved |
| Dagster job pods | `ticket` | Already excluded in PromQL; makes it explicit |

Net: the volume that pages goes *down* (78% CI/QA moves off the paging path
entirely, via Phase 1 alone), while the specific stateful and ingress workloads that
should have been paging all along start doing so.

---

## 8. Recommended sequence

Ordered so that each step is independently valuable and nothing depends on an
unfinished predecessor.

**Phase 1 — Stop the bleeding (no schema changes, days)**

1. **Enable the two disabled fallback rules** on "Grafana Prometheus QA - Slack
   Warnings Route" and "Grafana Prometheus CI - Slack Warnings Route", sending CI/QA
   alerts to the "CI/QA Slack Notifications" escalation policy (Slack-only, no
   schedule target). This is a two-field change to already-built objects and it
   removes 78% of alert volume from the paging path while preserving the signal.
   See §8.1 for why this, and not deferral, is the right shape. (§5.2)
1b. Add escalation levels to the "QA Non-Paging Escalation Policy", or delete it and
   move `MITx Online QA - Open edX - Redis` onto the CI/QA Slack policy. Today it has
   a service but no levels, so those CloudWatch alarms notify nobody. (§5.2)
1c. Confirm Slack channel `C0BK6BHUCDP` is a QA-appropriate channel distinct from
   `#devops-alerts` (`GBDLJJX51`), then turn off the account-wide "default alerts
   channel" toggle, which currently posts every alert to `#devops-alerts`
   unconditionally regardless of routing (documented at `rootly/__main__.py:513-521`).
   Without this the separation has no observable effect.
2. Delete `sh-test`, `exampleDeleteMe-EscalationPolicy`; finish `Cloudwatch - Critical`
   source setup. (§5.7)
3. Remove or fix the three dead `component`-matching rules in the Grafana Service
   Route. (§5.5)
4. Import the six unmanaged alert routes into `rootly/__main__.py`. (§5.6)

**Phase 2 — Close the label gap (weeks)**

5. Move `product`/`application`/`component` into `K8sGlobalLabels`. (§3.2a)
6. Extend `Component`; make it a strict enum (drop `| str`). (§3.2b, §7.2)
7. Reconcile `Services` vs `Application`, or collapse them. (§3.2c)
8. Label the 205 unlabeled production workloads, starting with
   `operations-production` and `data-production`. This is the long pole.

**Phase 3 — Wire labels into alerting (weeks, after Phase 2)**

9. Add `metricLabelsAllowlist` to kube-state-metrics. (§7.3 Option A)
10. Add `alert_tier` to the schema and set it per workload.
11. Rewrite the 18 EKS rules with `group_left` joins onto `kube_pod_labels`.
12. Replace the alertname demotion list with tier-based urgency rules. (§7.4)
13. Add component-level routing rules for the 19 orphaned services. (§5.3)

### 8.1 Decided: QA is notify-not-page, not deferred and not silenced

**Decision (2026-07-30, tmacey), resolving Q2:** QA alerts are kept, not silenced,
because QA failures frequently precede the same failure in production as changes are
promoted. They should reach the team without paging anyone.

The implementation consequence is that **Rootly's deferral path is the wrong tool
here**, even though it is the mechanism already in use for Medium urgency:

- A deferral path exists to suppress *pages*. The CI/QA destination is a Slack
  channel with no schedule target, so nothing pages in the first place — there is
  nothing for deferral to suppress.
- Deferral would actively damage the precursor use case. `after_deferral_behavior`
  is `re_evaluate`, so held alerts replay at 09:00 ET. That severs the alert from
  the QA deploy that caused it — the timing correlation *is* the signal — and
  converts a trickle into a 09:00 burst. At current volumes (~59 QA alerts/day by
  the 78%-of-1,070-over-14-days estimate) that burst is itself an alert-fatigue
  event.
- Slack retains history. An overnight QA alert is still there at 09:00 without any
  Rootly machinery holding it.

So: route CI/QA to the Slack-only escalation policy at delay 0, and do **not** add a
deferral path to it. "Deferred to business hours" is achieved by the destination not
being a pager, not by holding the message.

Open sub-decision, not blocking: should QA alerts also be excluded from the
`Medium` urgency band entirely (moved to `Low`)? Medium is what the production
deferral path matches. Leaving QA at Medium is harmless today because QA alerts
reach a different escalation policy that has no deferral path — but it couples two
unrelated things, and a future change to the Medium band would silently affect QA.
Recommend setting the QA and CI sources' default urgency to `Low` for decoupling,
with the notify behaviour coming from the route destination rather than the urgency.

This decision also removes the `environment != production → Medium` rule from the
proposed §7.4 urgency table; environment-based handling belongs in routing
(destination), not urgency (severity).

---

## 9. Open questions — all resolved 2026-08-17

> **§7, §8 and §9 of this document are superseded by
> [rootly-label-routing-implementation-spec.md](rootly-label-routing-implementation-spec.md)
> (2026-08-17).** The measurements in §0-§6 stand, with the corrections recorded in
> that spec's §0: the kube-state-metrics change needs *two* independent gates rather
> than one (the chart's own allow-list also drops `kube_*_labels`); §7.3's code sketch
> nests the Helm value one level too deep and would silently no-op; the join target
> differs per rule, so 16 of 20 EKS rules are rewritable rather than all 18; and §5.5's
> speculation that the `Grafana` source might be dead has since become true — PR #5400
> repointed the Synthetic Monitoring rules away from it (Q4 below).

**Q1. — RESOLVED 2026-08-17.** CI alerts keep reaching Rootly; do not drop them to
`oblivion` in the CI stack. CI ships no metrics, so the volume is zero — the hazard is
the CI source's **High** default urgency, which is fixed at the source (High → Low)
rather than by hiding the path. Keeping delivery visible in Rootly keeps it auditable.
See spec §1.1.

**Q2. — RESOLVED 2026-07-30 (tmacey).** QA alerts **notify without paging**, and are not
silenced: they often act as precursors to production issues as QA changes get promoted.
Implemented as Slack-only notification, delivered immediately, with no paging target and
deliberately **no** deferral path — §8.1 explains why deferral is the wrong tool here
even though it is the mechanism used for Medium urgency elsewhere.

**Q3. — RESOLVED 2026-08-17 (tmacey).** The demotion was **deliberate**. MySQL,
MongoDB, OpenSearch, Qdrant, ClickHouse and StarRocks are replicated enough that a
missing replica is not a wake-up. They are tiered `notify`, preserving today's
behaviour but expressing it per-workload rather than per-alertname. **This invalidates
§7.5's escalation column** for every stateful row and shrinks Phase 3's paging-volume
benefit to near zero — the remaining justification is routing precision and
maintainability, stated explicitly in spec §1.2.

**Q4. — RESOLVED 2026-08-17, by evidence.** The `Grafana` source (`f4d836c0`) is now
**inert**, and the "Grafana Service Route" bound to it is dead — as this section
originally suspected. It *was* fed by the three MIT Learn Synthetic Monitoring rules via
the UI-created `Rootly` contact point (`eel3rjpiwahoge`, the `grafana_webhooks`
endpoint), but **PR #5400 removed that override on 2026-08-13**; live provisioning API
confirms all six SM rules now carry `receiver: policy-tree`, routing through Pulumi's
`rootly` contact point to the production `alertmanager` source `90cda8ea` instead.

Those rules emit `service=mitlearn` and `component=webapp|api|nextjs`, so **two of the
three "dead" `component` rules would match today** if moved to the Grafana Production
Service Route — reaching `MIT Learn - API` and `MIT Learn - NextJS`, two of the 19
orphaned services in §5.3, with no label-pipeline work at all. Move them; delete the
route they sit on. See spec §0.4 and step P1.5.

**Q5. — RESOLVED 2026-08-17 (tmacey).** Keep both, with distinct jobs: `ou` is the
cost-allocation axis, `product` is the alerting/ownership roll-up that maps onto the
Rootly service catalog. No enum collapse. One fix: drop
`BusinessUnit.residential_staging` (§3.2e). See spec §1.3.

**Q6. — RESOLVED 2026-08-17 (tmacey).** Central backfill by the infrastructure team,
plus a CI gate with a shrinking allowlist so the gap cannot reopen. Infra already owns
most of the unlabeled workloads (`operations-production` 97%, `data-production` 96%).
Phase 2 is weeks, not quarters. See spec §1.4, §3.3, §3.4.
