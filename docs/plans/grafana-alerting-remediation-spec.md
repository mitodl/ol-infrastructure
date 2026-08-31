# Grafana alerting remediation — implementation spec

Date: 2026-08-07
Supersedes the *recommendations* (§6) of
[grafana-alerting-holistic-analysis.md](grafana-alerting-holistic-analysis.md).
The analysis's *measurements* stand; several of its conclusions do not, and §0 below
records exactly which and why.

Everything in this spec was verified against live state on 2026-08-07: the production
Mimir/Loki rulers, the Cloud Alertmanager config API, the Rootly API, and the
`pulumiverse_grafana` / `pulumi_rootly` provider SDKs vendored in this repo.

---

## 0. Corrections to the analysis

Five findings changed materially once checked against live state. Each one changes what
the corresponding workstream should actually do.

### 0.1 The orphaned ruler set is 10 rules, not 170 — and most of what is in the ruler must NOT be deleted

`GET /api/ruler/grafanacloud-prom/api/v1/rules` returns four namespaces:

| Namespace | Groups | Owner | Action |
|---|---:|---|---|
| `asserts` | 70 | **Grafana Cloud (Asserts plugin)** | **Do not touch** |
| `integrations-kubernetes` | 25 | **Grafana Cloud (k8s integration)** | **Do not touch** |
| `eks` | 1 (6 rules) | cortextool era, orphaned | Delete |
| `linux-host` | 3 (4 rules) | cortextool era, orphaned | Delete |

The analysis's "170 metric rules" counted the vendor-managed `asserts` and
`integrations-kubernetes` namespaces. Deleting those would remove Asserts and the
Kubernetes integration wholesale. **The cortextool-era orphans under our control are
exactly 10 metric rules in two namespaces**, plus 28 Loki rules in five namespaces
(`5xx-errors`, `cert-manager`, `edxapp-logs`, `heroku-logs`, `vault`).

Everything the analysis said about those 10 is confirmed verbatim:

- `DeploymentUnavailableWarning` filters `.*-(production)`, `DeploymentUnavailableCritical`
  filters `.*-(ci|qa)` — **inverted**, and both use
  `kube_deployment_status_condition{condition="Available", status="false"}`, the query
  `eks_general.py:88-116` deliberately replaced.
- `DeploymentReplicasMissingWarning` carries `severity: "Warning"` (capital W).
- `CPUUsageWarning` queries `host="$instance"` — an unsubstituted template variable.

### 0.2 Deleting them creates no coverage hole — verified rule by rule

Every one of the 10 has a Pulumi-managed equivalent already deployed:

| Orphaned ruler rule | Pulumi equivalent |
|---|---|
| `DaemonsetReplicasMissing{Warning,Critical}` | `eks_general.py:31,44` |
| `DeploymentReplicasMissing{Warning,Critical}` | `eks_general.py:59,72` |
| `DeploymentUnavailable{Warning,Critical}` | `eks_general.py:117,132` |
| `CPUUsageWarning` | `linux_host.py:29` |
| `MemoryUsageWarning` | `linux_host.py:56` |
| `DiskUsage{Warning,Critical}` | `linux_host.py:79,92` |

Same for the 28 Loki rules against `log_rules/`. Deletion is pure subtraction of
duplicate-and-wrong delivery.

### 0.3 The legacy Cloud Alertmanager route tree is live — confirmed

`GET /api/alertmanager/grafanacloud-ngalertmanager/config/api/v1/alerts` returns its own
`rootly` receiver and this route tree, including the `Deploy.*` silence the Pulumi tree
lacks:

```
channel="notifications-ocw-misc" → oblivion
alertname=~"Kube.*"              → oblivion
alertname=~"Deploy.*"            → oblivion     ← not in the Pulumi tree
severity="warning"               → rootly
severity="critical"              → rootly
```

Consequence worth noting before deleting: the `Deploy.*` silence is what currently
suppresses the four inverted `Deployment*` orphans. Of the 10 orphaned metric rules,
only 5 actually deliver today (`Daemonset*` ×2, `DiskUsage*` ×2, `MemoryUsageWarning`);
`CPUUsageWarning` is dead and the four `Deployment*` are silenced.

### 0.4 The CI/QA→Slack diversion already shipped — and is inert. This is a live defect.

PR #5082 (2026-07-22) created `CI/QA Slack Notifications` escalation policy → Slack
`#devops-warnings`, and two alert routes pointing CI and QA Grafana sources at it. Live
state:

```
Grafana Prometheus QA - Slack Warnings Route   enabled: true
  └─ Fallback Rule ...                         enabled: FALSE
Grafana Prometheus CI - Slack Warnings Route   enabled: true
  └─ Fallback Rule ...                         enabled: FALSE
```

The routes are enabled; **their only rule is disabled**, so nothing matches and CI/QA
alerts fall through to the default escalation policy. `pulumi_rootly`'s
`AlertRouteRuleArgs` exposes only `(condition_groups, destinations, fallback_rule, name,
position)` — **there is no `enabled` field**, so Pulumi cannot fix this and an apply will
not detect it. The in-repo comment at `saas/rootly/__main__.py:3310-3314` already flags
this; nobody has connected it to the fact that it makes the diversion a no-op.

This is the single highest-leverage item in the whole project and it is a two-click UI
change, not an engineering task.

### 0.5 "QA pages at production urgency" is true but for a different reason, and `warning → Low` does not stop paging

Rootly's three urgency tiers are High / Medium / Low. Measured:

- **CI** source default urgency: **High**, zero urgency rules. CI alerts arrive at the
  *highest* tier — worse than the analysis reported.
- **QA** source default: **Medium**, zero urgency rules.
- **Production** source default: High, with **12 urgency rules already in place** (added
  2026-07-20, `saas/rootly/__main__.py:2937-2980`) demoting `severity=warning` → Low and
  ten named `*Critical` alertnames → Medium.

So QA (Medium) and production `HPAAtMaxReplicasCritical` (Medium, via rule
`4ee07b26`) do land on the same tier — but because production's noisy four were
*deliberately demoted*, not because severity is ignored downstream.

Critically: **the Default Escalation Policy has no path conditioned on Low urgency.**
The only urgency-conditioned path is `defer-medium-urgency-off-hours`
(`saas/rootly/__main__.py:656`), which holds *Medium* outside business hours. Low
urgency therefore pages exactly like High. The analysis's step 2 — "route warning to a
Rootly low-urgency path" — would relabel alerts without silencing a single page.

There is a real non-paging destination available: `QA Non-Paging Escalation Policy`
(`d63b7456-0d9f-44e8-80a5-4fc3df7e986b`, zero escalation levels), plus the Slack-only
`CI/QA Slack Notifications` policy (`b32c5938-4eb2-446f-a766-ab54970cf0bf`).

### 0.6 `courses-backend.learn.mit.edu` at 33% is a near-idle host, not a chronic outage

Measured over the last 24h in production Mimir:

| Host | 5xx rate | Traffic |
|---|---:|---:|
| `courses-backend.learn.mit.edu` | 33.3% | **~3 requests/day** (1 single 500) |
| `api.mitxonline.mit.edu` | 18.0% | 4.96 req/min — **1,288 × HTTP 500/day** |
| `api.learn.mit.edu` | 1.09% | 4,759 req/min |
| `opik.ol.mit.edu` | **0%** | 25.7 req/min (recovered since the analysis window) |

`courses-backend` sitting in the analysis's "chronic double-digit failure" table is an
artifact of a three-request denominator. Listing it as a coverage gap would have led
directly to a permanently-firing alert. This is the concrete reason the SLO rules in
Workstream 4 need a minimum-traffic gate, and the number that calibrates it.

Also worth recording for Workstream 3's investigation: every `api.mitxonline.mit.edu`
5xx is code **500**, not 502 — an application error, not a gateway/upstream timeout.

### 0.7 QA stack — same defects, three differences that change scope

Every check in §0.1-0.3 was re-run against the QA stack on 2026-08-07. QA is
**identical to production** on everything W1 touches:

| Check | Production | QA |
|---|---|---|
| Mimir ruler namespaces | `asserts` 70 grp · `integrations-kubernetes` 25 · **`eks` 6 rules** · **`linux-host` 4** | `asserts` 68 grp/614 · `integrations-kubernetes` 25/124 · **`eks` 6** · **`linux-host` 4** |
| Loki ruler orphans | 5 namespaces, 28 rules | 5 namespaces, 28 rules — same names |
| Legacy Cloud Alertmanager | own `rootly` receiver + `Deploy.*` silence | **byte-identical route tree** |
| `eks` inverted filters | `Unavailable{Warning→prod, Critical→ci\|qa}` | same |
| `DeploymentReplicasMissingWarning` | `severity: "Warning"` (capital W) | same |
| Pulumi rule set | 24 `infrastructure-alerts` + 28 `log-alerts` | same counts |

So **W1 applies to QA unchanged** — same seven namespaces, same Alertmanager reset, same
vendor namespaces to leave alone.

Three differences that do change scope:

**(a) The orphans bite differently on QA, and there the inverted filters hit real data.**
Each stack's Mimir tenant holds only its own clusters, so on production the legacy
`.*-(ci|qa)` rules match nothing. On QA they match — the legacy
`DeploymentUnavailableCritical`, which is the *inverted* one, is the copy that evaluates
against live data. It is silenced by the `Deploy.*` route, so it does not deliver, but it
is the case where the inversion is not merely theoretical. `DaemonsetReplicasMissingWarning`
(`.*-(ci|qa)`, lowercase severity, not matched by `Deploy.*`) does deliver on QA.

QA's *Loki* orphans are the mirror image: all hardcoded `cluster="applications-production"`,
evaluating a production selector against the QA tenant. They can never match — dead, but
still evaluated every interval. Confirmed on `5xx-errors`; the analysis reported this and
it holds.

**(b) QA's contact points are clean — the mess is production-only.** QA has six contact
points and no duplicates. Verified directly on production:

| Contact point | uid | Note |
|---|---|---|
| `rootly` | `bfsoqo63lsyrka` | Pulumi |
| `Rootly` | `eel3rjpiwahoge` | **UI-created duplicate** |
| `OpsGenie` | `U4nPIcO7z` | **stale** |
| `OpsGenie Ops Team` | `-1rdMF27z` | **stale** |

None of the three offenders exist on QA. **W5b's contact-point cleanup is production-only.**

**(c) Synthetic Monitoring does not exist on QA.** Production has folder
`grafana-synthetic-monitoring-app` with **6** rules (not 7 as reported), of which **3**
carry `notification_settings.receiver: "Rootly"` — pointing at the UI duplicate. The other
3 carry no override and no severity, so they drop to `oblivion`. QA has no such folder.
**W5b is production-only.**

### 0.8 A fifth rule source the analysis's pipeline table missed

Production has folder `GrafanaCloud` (`_iIDezonz`) holding one active rule,
`GrafanaMetricCount` — `severity: warning`, `for: 5m`, `dont_resolve: true`, no receiver
override, `isPaused: false`. Under the notification policy `severity=warning` routes to
`rootly`, so this is a live delivery path outside all four pipelines the analysis
enumerated. It did not fire in the 30-day window. QA does not have it.

Not urgent, but it belongs on the inventory: fold it into the W6 re-measurement rather
than treating the four-pipeline table as complete.

### 0.9 QA gives almost no calibration signal for the APISIX SLO rules

QA has 31 APISIX hosts. Measured 1d, 2026-08-07:

| Host | 5xx | req/min |
|---|---:|---:|
| `mitx-qa.mitx.mit.edu` | 0% | 97.1 |
| `courses.rc.learn.mit.edu` | 0% | 77.3 |
| `api.rc.learn.mit.edu` | 0% | 16.1 |
| `opik-qa.ol.mit.edu` | 0.59% | 11.8 |
| `courses-rc.xpro.mit.edu` | 0% | 8.1 |
| `courses-backend.rc.learn.mit.edu` | **21.9%** | **0.022** (~32 req/day) |
| *22 further hosts* | 0% | < 0.5 |

Two things follow. First, `courses-backend.rc.learn.mit.edu` independently reproduces the
production `courses-backend` artifact — a double-digit ratio on a near-idle denominator,
on a different stack, for a different host. The minimum-traffic gate is not a
production-specific workaround.

Second, the `> 0.01` req/s gate calibrated on production volumes silences roughly 22 of
QA's 31 hosts, including `api.rc.mitxonline.mit.edu` (0.026 req/min). That is the correct
outcome — those hosts are idle — but it means **deploying the SLO rules to QA produces
almost no firing data to calibrate against.** Calibrate on production, in the non-paging
mode W4 specifies. Do not treat a quiet QA as evidence the thresholds are right.

### 0.10 CI stack — not a third identical stack. Check before deleting.

CI was checked on 2026-08-07 and diverges from production/QA in ways that matter for W1.

**CI's `eks` ruler namespace holds 20 rules, not 6 — and they are the *current, correct*
rule set.** Not the stale cortextool six. The full modern set is there —
`PodOOMKilled*`, `PodCrashLooping*`, `HPAAtMaxReplicas*`, `StatefulSetReplicasMissing*`,
`NodeNotReady*`, `CeleryBeatPodRestarts*`, `KubernetesJobFailed*` — with **correct**
environment filters (`Unavailable{Warning→ci|qa, Critical→production}`, i.e. *not*
inverted) and lowercase severities throughout. Someone or something pushed the modern
rule set into CI's Mimir ruler.

So CI's ruler duplication is a different animal: it is a faithful duplicate of the
Grafana-managed Pulumi rules rather than a drifted, wrong one. It still double-delivers
and should still be deleted, but none of §0.1's "stale and provably wrong" argument
applies there. **Do not reuse the production justification when deleting on CI.**

**The vendor namespaces are nearly absent on CI:** `asserts` is 2 groups / 2 rules (vs
68-70 groups on QA/production) and there is **no `integrations-kubernetes` namespace at
all**. The don't-touch rule still stands, but the blast radius of a mistake is small
there — which is precisely why CI is the wrong stack to build confidence on before
running the same command against production.

Everything else matches: identical legacy Cloud Alertmanager route tree with its own
`rootly` receiver and `Deploy.*` silence; the same 5 Loki orphan namespaces / 28 rules;
the same Pulumi set (24 `infrastructure-alerts` + 28 `log-alerts`); clean contact points
(6, no duplicate `Rootly`, no OpsGenie) exactly like QA.

CI also has **no `grafana-synthetic-monitoring-app` folder, no `GrafanaCloud` folder, and
no `grafanacloud-ml` folder** — it carries the `Adaptive Traces` contact point but zero
Adaptive Traces rules, where production and QA have 5 each.

### 0.11 CI produced zero genuine alerts in 30 days — and the reason is a latent defect in all three stacks

CI's entire 30-day alert state history is 536 entries:

| State | Count |
|---|---:|
| `Normal (NoData)` | 219 |
| `Pending (Error)` | 159 |
| `Normal (Updated)` | 113 |
| `Alerting (Error)` | **45** |
| plain `Alerting` | **0** |

**Not one rule condition actually matched on CI in 30 days.** All 45 "firings" are
`Alerting (Error)` — evaluation failures, one per rule, all
`failed to build query 'A': data source not found`, all dated ~2026-07-09. All 52 CI
rules report `health: ok` today, and CI's datasource UIDs (`grafanacloud-prom`,
`grafanacloud-logs`) resolve correctly, so this was a transient episode — most likely a
provisioning-order race on first deploy — that self-resolved.

The durable finding is *why a transient datasource blip became 45 alerts*:

> **`exec_err_state` is never set on any rule in this program** — 0 occurrences across
> `metric_rules/` and `log_rules/`, against 60 for `no_data_state`. Grafana defaults
> `exec_err_state` to `Error`, which escalates an evaluation failure into a notifying
> Alerting state carrying the rule's own `severity` label.

So every rule in all three stacks pages on evaluation error, not just on the condition it
was written for. On CI that turned one datasource hiccup into 45 alerts at CI's **High**
urgency default (§0.5), with the Slack diversion inert (§0.4). The same blip on
production would page the on-call 52 times.

This is deliberate care taken over `no_data_state` with its counterpart left at a
paging default. Tracked as its own task; the fix is to set `exec_err_state` explicitly
(`"OK"` for the CI/QA-filtered warning tier, `"Error"` or `"KeepLast"` for production
criticals — decide per tier) rather than leaving it implicit.

---

## 1. Revised sequence

Ordered by benefit-to-risk with the corrections applied. W0 is new and jumps the queue
because it is a UI toggle that removes more paging volume than anything else here.

| # | Workstream | Effort | Paging volume removed |
|---|---|---|---|
| W0 | Enable the two disabled CI/QA route rules | minutes (UI) | up to 621/30d |
| W1 | Delete the 10 + 28 orphaned ruler rules and the legacy Alertmanager config | small | duplicate delivery |
| W2 | Give Low urgency a non-paging path; fix CI's High default | small | the rest of non-prod |
| W3 | Fix the four rules producing 92% of volume | medium | ~1,135/30d → est. low hundreds |
| W4 | APISIX edge SLO alerting | medium | *adds* coverage (the point) |
| W5 | Reclaim the latency signal; fix the SM rules | small | adds coverage |
| W6 | Rootly `noise` classification | ongoing | enables the next round |
| W7 | ML pilot — gated on W0-W4 | large | evaluate only |

---

## W0 — Enable the disabled CI/QA route rules

**Not a code change.** In the Rootly UI, enable the single fallback rule on each of:

- `Grafana Prometheus QA - Slack Warnings Route` (route `691913ad`, rule `1e552e8f`)
- `Grafana Prometheus CI - Slack Warnings Route` (route `588edbfc`, rule `53e8784f`)

Both already point at `CI/QA Slack Notifications` → `#devops-warnings` (`C0BK6BHUCDP`),
which has one Slack-channel level and no paging level.

Because `AlertRouteRuleArgs` has no `enabled` field, this state cannot be enforced or
detected from Pulumi. Add to `saas/rootly/__main__.py` above the two route resources a
comment recording that the live `enabled` flag is load-bearing and unmanaged, and add a
check to whatever periodic audit exists — `GET /v1/alert_routes` and assert
`.rules[].enabled == true` for these two rule IDs.

**Verify:** trigger or wait for one QA alert; confirm it appears in `#devops-warnings`
and that no page is raised.

---

## W1 — Delete the orphaned ruler rules and the legacy Alertmanager config

Delete **only** these namespaces. Do not pass a wildcard.

```
# Mimir ruler — production, QA, and CI stacks
DELETE /api/ruler/grafanacloud-prom/api/v1/rules/eks
DELETE /api/ruler/grafanacloud-prom/api/v1/rules/linux-host

# Loki ruler — production, QA, and CI stacks
DELETE /api/ruler/grafanacloud-logs/api/v1/rules/5xx-errors
DELETE /api/ruler/grafanacloud-logs/api/v1/rules/cert-manager
DELETE /api/ruler/grafanacloud-logs/api/v1/rules/edxapp-logs
DELETE /api/ruler/grafanacloud-logs/api/v1/rules/heroku-logs
DELETE /api/ruler/grafanacloud-logs/api/v1/rules/vault
```

**Never** `asserts` or `integrations-kubernetes` (§0.1).

Then reset the Cloud Alertmanager to a default/empty config so its `rootly` receiver and
route tree stop delivering:

```
DELETE /api/alertmanager/grafanacloud-ngalertmanager/config/api/v1/alerts
```

Order matters: delete the rules first, then the Alertmanager config. Reversing it leaves
rules firing into a default receiver for the gap.

**Pre-flight:** snapshot all seven namespaces to a file before deleting — these are
unmanaged and unrecoverable otherwise.

**Verify:**
1. `GET .../config/api/v1/alerts` no longer lists a `rootly` receiver.
2. The two rulers return only `asserts` / `integrations-kubernetes` (Mimir) and empty (Loki).
3. Over the following 24h, no Rootly alert has an `external_url` that is an Explore
   deeplink — that shape is the ruler-generated signature (analysis §3.1).

**Do this on all three stacks, but not with one script.** QA is rule-for-rule identical
to production (§0.7). **CI is not** (§0.10): its `eks` namespace holds 20 rules — the
*current, correct* set, not the stale six — and it has no `integrations-kubernetes`
namespace with an `asserts` of only 2 rules. The namespace names to delete are the same
on all three; the justification and the expected before/after are not. Re-read §0.10
before touching CI, and do not treat a clean CI run as evidence the production run is
safe — the vendor namespaces that make production risky are nearly absent on CI.

---

## W2 — Make severity mean something

Two independent defects, both in `src/ol_infrastructure/saas/rootly/__main__.py`.

**2a. CI's default urgency is High.** Change
`alerts_source_grafana_prometheus_ci` (`:2918`) from
`5d357977-…` (High) to `d7ed8e91-…` (Low). CI is never a paging environment. With W0
enabled this is belt-and-braces, but it is the correct declared state and it is the
value that applies if a route rule is ever disabled again.

**2b. Low urgency has no non-paging path.** Add an `EscalationPath` on the Default
Escalation Policy (`96629210-…`) modelled on `escalation_path_defer_medium_urgency_off_hours`
(`:656`) but for Low, with **no time-window rule** — Low should never page, at any hour:

```python
escalation_path_low_urgency_no_page = rootly.EscalationPath(
    "low-urgency-no-page",
    name="Low urgency does not page",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    path_type="deferral",
    match_mode="match-all-rules",
    after_deferral_behavior="re_evaluate",
    rules=[
        {
            "ruleType": "alert_urgency",
            "urgencyIds": ["d7ed8e91-ffa9-4cc4-b524-729d14a4425b"],
        },
    ],
    opts=rootly_opts,
)
```

Confirm with Rootly support or a live test whether a deferral path with no
`deferral_window` holds indefinitely or is rejected; if rejected, use an all-day
seven-day window, which is equivalent in effect. **This must be verified before W4's
calibration mode depends on it.**

Only once 2b exists does the production source's existing `severity=warning → Low` rule
(`:2939`) actually mean "do not page."

**Deliberately not doing:** splitting `warning` and `critical` onto different contact
points in `alertmanager.py`. Grafana already emits `severity` as a label; Rootly already
routes on `$.commonLabels.severity`. Adding a second Rootly webhook contact point would
duplicate the urgency logic in two systems. Keep severity semantics in Rootly, where the
escalation paths live.

---

## W3 — Fix the four rules producing 92% of volume

All changes in `metric_rules/eks_general.py` and `alertmanager.py`. Both provider
features below were confirmed present in the vendored SDK.

**3a. `keep_firing_for` — confirmed supported.** `alerting.RuleGroupRuleArgs` accepts
`keep_firing_for` (verified by introspection; also `missing_series_evals_to_resolve`,
which is directly relevant — when a pod is replaced its series vanishes, resolving the
alert instantly and re-firing under the new pod name).

Add to `PodOOMKilled*` and `PodCrashLooping*`:

```python
keep_firing_for="30m",
missing_series_evals_to_resolve=10,
```

`keep_firing_for` holds the alert through the gap between a pod dying and its
replacement OOMing; `missing_series_evals_to_resolve` stops the vanished series from
resolving-and-refiring. Together these attack the §3.3 storm at its actual mechanism.

**3b. Per-route grouping override — confirmed supported.**
`NotificationPolicyPolicyArgs` accepts `group_bies`, so a child route can override the
root grouping without reverting the deliberate `alertmanager.py:99-122` fix. Add a
policy branch **above** the `severity=warning` / `severity=critical` branches:

```python
alerting.NotificationPolicyPolicyArgs(
    matchers=[
        alerting.NotificationPolicyPolicyMatcherArgs(
            label="alertname",
            match="=~",
            value="Pod(OOMKilled|CrashLooping)(Warning|Critical)",
        )
    ],
    contact_point="rootly",
    continue_=False,
    # Deliberately drops `pod` and `container`: a churning workload mints a
    # new pod name per restart, and the root grouping then mints a new Rootly
    # alert per name. Grouping at the deployment level reports the workload
    # once. See the analysis, section 3.3.
    group_bies=["alertname", "grafana_folder", "cluster", "namespace", "deployment"],
    group_interval="30m",
    repeat_interval="12h",
),
```

Caveat to check on first apply: Grafana requires `alertname` and `grafana_folder` in any
policy-level `group_by` and may silently re-add them — diff the preview.

Note the OOM rules currently aggregate `by (cluster, namespace, pod, container)` and do
**not** emit a `deployment` label, so grouping on `deployment` collapses to the
namespace level as written. Either accept namespace-level grouping (adequate — the
observed storm was a single deployment in a single namespace) or add
`* on (namespace, pod) group_left(deployment)` against
`kube_pod_owner`/`kube_replicaset_owner` to carry the label through. Prefer accepting
namespace-level first; it is one line and no new join.

**3c. `HPAAtMaxReplicas` — stop paging on it.** At-max is a capacity fact.
`keda-hpa-traefik-gateway-controller` fired 122×/30d; that is one resize decision, not
122 incidents. Two options, in preference order:

1. Move it off the paging path entirely: add `channel: notifications-ocw-misc`-style
   routing to a `#devops-warnings` Slack contact point, or set its label to
   `severity: warning` so W2's Low path catches it.
2. Gate it on a saturation co-signal (HPA at max **and** target metric above threshold).
   Better alert, materially more work, needs a per-workload decision on what "saturated"
   means. Defer to a follow-up.

Take option 1 now.

**3d. `DeploymentUnavailable`** needs no rule change — the Pulumi version is already the
corrected one (`eks_general.py:88-116`). Its 26 prod firings are largely the orphaned
ruler copy, which W1 removes. **Re-measure after W1 before touching it.**

---

## W4 — APISIX edge SLO alerting

New file: `src/ol_infrastructure/infrastructure/grafana_alerting/metric_rules/apisix_edge.py`,
registered from `metric_rules/base.py:147` alongside `eks_general` and `linux_host`.

`apisix_http_status{matched_host, code}` is confirmed present in production Mimir with
the labels the analysis claimed.

### The minimum-traffic gate is not optional

Per §0.6, `courses-backend.learn.mit.edu` runs at 33% 5xx on three requests per day. A
bare ratio alert fires on it forever. Gate on absolute rate in the same window:

- `courses-backend`: 0.000035 req/s
- `api.mitxonline` (the host we must catch): 0.083 req/s

A gate of `> 0.01` req/s separates them by a factor of ~288 in one direction and ~8 in
the other. Comfortable.

### Rules

Two windows, both gated. Written to fit the existing `rd()` three-stage pipeline
(threshold baked into the PromQL, `condition="C"`).

```python
_RATIO = (
    'sum by (matched_host) (rate(apisix_http_status{{code=~"5.."}}[{w}]))'
    " / "
    "sum by (matched_host) (rate(apisix_http_status[{w}]))"
)
_GATE = "sum by (matched_host) (rate(apisix_http_status[{w}])) > 0.01"
```

**Fast — a cliff.** `(ratio[10m] > 0.05) and (gate[10m])`, `for_="5m"`.
Catches `opik`-shaped failures within ~15 minutes.

**Slow — a creep.** `(ratio[6h] > 0.01) and (gate[6h])`, `for_="30m"`.
This is the rule that would have caught `api.mitxonline` in week one, when it crossed
from 0% to 5.5%.

Write the gate clause **first** in the `and` expression. PromQL's `and` carries the
left-hand side's value through, and the pipeline's stage C fires on `last(A) > 0` — a
ratio on the left works, but the gate on the left is the safer idiom and matches the
reasoning already documented at `eks_general.py:108-116`.

### Calibration — two weeks, non-paging

Ship both with `severity: warning`. On the production stack that maps to Rootly Low,
which is only genuinely non-paging **after W2b lands** — W2b is a hard prerequisite, not
a nice-to-have. If W2b slips, route these to Slack via a dedicated contact point
instead; do not ship them paging.

Review at two weeks: expected steady-state firers are `api.mitxonline` (until the
regression in `tk-investigate-…-e12759` is fixed) and nothing else. `api.learn.mit.edu`
at 1.09% will sit just above the slow threshold — decide then whether to raise the slow
threshold to 2% or accept it as a genuine signal (304,860 absolute 502s over 30 days
argues it is genuine).

Promote to `severity: critical` only after a clean review.

### Explicitly out of scope for now

True Google-style multi-window burn-rate alerting needs an explicit SLO target per host
and recording rules to make 6h/3d windows cheap. Neither exists. The two-threshold form
above delivers the coverage; revisit burn-rate proper once host tiers are defined.

---

## W5 — Reclaim the latency signal and fix the SM rules

**5a. `HTTPRequestDurationTooHighAvg [5m]`** fired 1,168×/30d into oblivion, currently
reporting `api.learn.mit.edu/learn/health` at 4,141 ms. It has `for: 0s` and no
`severity`. Give it `for_="10m"` and `severity: warning` (non-paging after W2b), then
re-measure for two weeks before considering critical. Do not delete it.

Also: a 4.1-second health endpoint on the highest-traffic host in the estate is worth a
separate investigation task regardless of the alerting outcome.

**5b. Synthetic Monitoring rules — production only** (§0.7c; QA has no SM folder).
Folder `grafana-synthetic-monitoring-app` holds **6** rules, hand-created in the UI and
invisible to Pulumi. **3** bypass the notification policy via
`notification_settings.receiver: "Rootly"` (the UI-created duplicate, uid
`eel3rjpiwahoge`, distinct from Pulumi's `rootly`, uid `bfsoqo63lsyrka`): `Learn API
Health Endpoint`, `Learn NextJS Homepage (Bypass Fastly)`, `Learn Homepage`. The other 3
carry no override and no severity, so they drop to `oblivion` — decide whether they are
worth keeping at all.

`alerting.RuleGroupRuleArgs` accepts `notification_settings`, so these can be adopted
into Pulumi as-is. Do that, then fix the two substantive defects:

- `avg_over_time(probe_success[5m]) < 1` pages on any single failed probe out of five
  (observed firing at 0.8). Change to `< 0.6`.
- The `summary` annotation on `Learn NextJS Homepage (Bypass Fastly) - Check Failed`
  reads "response times 30% Greater Than Normal" while the query measures availability.
  Rewrite it to describe what fired.

Once the SM rules point at Pulumi's `rootly`, delete the duplicate `Rootly`
(`eel3rjpiwahoge`) and the two stale OpsGenie contact points (`OpsGenie` `U4nPIcO7z`,
`OpsGenie Ops Team` `-1rdMF27z`). All three are production-only — QA is already clean.

---

## W6 — Rootly `noise` classification

All 1,449 alerts are `noise: not_noise` because nobody has ever set the field. Start
classifying during triage. This produces the data for the next tuning round and is a
prerequisite for Rootly's own grouping/suppression being useful. No engineering work;
it is a triage-habit change.

Re-run the analysis's measurement queries (its Appendix) 30 days after W3 lands to get a
clean before/after.

---

## W7 — ML pilot

**Gated on W0-W4 landing and a 30-day clean measurement.** Unchanged from the analysis's
step 6, with its caveat intact and now sharpened by §0.6: an anomaly model trained on
`courses-backend` learns three-requests-a-day as normal, and one trained on
`api.mitxonline` at 18% learns 18% as normal. ML answers "did this change?", never "is
this acceptable?" It supplements W4's absolute-level SLO alerting; it never replaces it.

Scope when it starts: 2-3 forecast jobs, Slack only, 30 days, measured against known
incidents. The 944-firing `adaptive_traces_forecast_learn_webapp` job is the control
group for what skipping that step produces.

Cost is unquantified. `AGENTS.md` records Synthetic Monitoring being rejected at
~$3,200/mo; get a quote against the current contract before committing.

---

## Open decisions

1. **W2b mechanism.** Does a Rootly deferral path with an urgency rule and no
   `deferral_window` hold indefinitely, or must it carry an all-day/seven-day window?
   Blocks W4's calibration mode. Verify against the API before building on it.
2. **W3b grouping label.** Accept namespace-level grouping for the OOM/crashloop route,
   or add the `kube_pod_owner` join to carry `deployment` through? Recommendation:
   accept namespace-level now; the observed storm was one deployment in one namespace.
3. **W4 slow threshold vs `api.learn.mit.edu`.** 1.09% sits just above a 1% slow
   threshold on the largest host in the estate. Decide at the two-week review with real
   firing data rather than pre-emptively.
4. **W0 enforcement.** Given `pulumi_rootly` cannot manage per-rule `enabled`, where
   does the assertion live — a scheduled check, or accepted as unenforced with a
   comment? Recommendation: a scheduled check; this defect was invisible for 16 days.
5. **`exec_err_state` policy per severity tier.** Setting it explicitly is not in
   dispute (§0.11); what to set it *to* is. `"OK"` silences a class of genuine
   breakage — a rule that cannot evaluate is not a healthy rule — so blanket `"OK"`
   trades a paging bug for a blind spot. Suggested split: `"OK"` for the CI/QA-filtered
   warning tier, `"KeepLast"` for production criticals, plus one deliberate
   `DatasourceError`-style rule so evaluation failure is still visible somewhere. Needs
   a decision before W3 touches the rule bodies.

---

## Task mapping

| Task slug | Workstreams |
|---|---|
| `tk-delete-the-orphaned-cortextool-era-ruler-rules-a-3ffc34` | W1 |
| `tk-make-severity-mean-something-split-warning-criti-27f976` | **W0**, W2 |
| `tk-fix-the-four-rules-that-produce-92-of-all-grafan-02e7f0` | W3 |
| `tk-add-apisix-edge-burn-rate-slo-alerting-the-bigge-db03d1` | W4 |
| `tk-reclaim-the-discarded-latency-signal-and-fix-the-2c97ee` | W5 |
| `tk-start-classifying-rootly-alerts-with-the-noise-f-a1bd16` | W6 |
| `tk-pilot-grafana-cloud-ml-narrowly-2-3-jobs-slack-o-15ab98` | W7 |
| `tk-investigate-the-api-mitxonline-mit-edu-5xx-regre-e12759` | independent — see §0.6 (all HTTP 500, not 502) |

W0 did not exist as a task before this spec and belongs at the front of
`tk-make-severity-mean-something-…`, whose current description assumes CI/QA→Rootly
routing that was already meant to be gone.
