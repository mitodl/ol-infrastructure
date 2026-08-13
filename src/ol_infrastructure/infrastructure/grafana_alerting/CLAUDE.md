# grafana_alerting — architecture reference

This Pulumi program manages alerting and uptime monitoring for MIT Open Learning.
It replaces two legacy systems:
- **Pingdom (manual)** → Pingdom checks managed via a Pulumi dynamic provider (`pingdom_checks.py`)
- **grafana-alerts repo + cortextool** → Grafana-managed alert rules and Alertmanager config

Grafana Synthetic Monitoring was evaluated as an alternative for uptime checks but
ruled out due to cost (~$3,200/month at 4 probe regions × 1-minute polling cadence).

---

## Grafana Cloud stacks

There are three separate Grafana Cloud stacks, one per environment:

| Stack | Secrets file |
|---|---|
| CI | `src/bridge/secrets/grafana_cloud/api.ci.yaml` |
| QA | `src/bridge/secrets/grafana_cloud/api.qa.yaml` |
| Production | `src/bridge/secrets/grafana_cloud/api.production.yaml` |

Every stack provisions its own Mimir and Loki datasources under the same
generic UIDs: `grafanacloud-prom` (Mimir) and `grafanacloud-logs` (Loki).
The per-stack slugs shown in the UI (e.g. `grafanacloud-mitolci-prom`) are
datasource *names*, not UIDs — using them as UIDs fails with
"data source not found".

Each stack has its own Mimir metrics backend, its own Alertmanager, and its own
set of Grafana-managed alert rules. The Pulumi stacks (CI, QA, Production) map
1:1 to the Grafana Cloud stacks.

Secrets files must be encrypted with SOPS before committing:
```
sops --encrypt --in-place src/bridge/secrets/grafana_cloud/api.<env>.yaml
```

---

## Alert pipeline (end to end)

```
Grafana Alloy (on EKS / EC2)
    │  scrapes metrics and ships to Mimir
    ▼
Mimir (per-stack metrics storage, exposed as a Prometheus datasource)
    │  Grafana evaluates RuleGroup rules against Mimir every N seconds
    ▼
Grafana Alertmanager
    │  matches fired alerts against the NotificationPolicy route tree
    ▼
Rootly webhook  ──or──  Slack (#notifications-ocw-misc)
```

**Grafana-managed vs Mimir ruler-managed rules**

The old cortextool approach pushed rules directly to Mimir's ruler API. Rules
were evaluated inside Mimir independently of Grafana. This program uses
Grafana-managed rules instead (`alerting.RuleGroup`), where Grafana itself
evaluates rules by querying Mimir as a datasource. The end result (alerts →
Alertmanager → Rootly) is identical; only the evaluation path differs.

**CloudWatch alerts (out of scope here)**

RDS and ElastiCache components across many app stacks emit CloudWatch alarms
that route to SNS topics (`OpsGenie_Critical_Notifications` /
`OpsGenie_Warning_Notifications` — misleadingly named, actually webhook to
Rootly). That path is independent of Grafana and is managed in
`src/ol_infrastructure/infrastructure/monitoring/`.

---

## File structure

| File | Responsibility |
|---|---|
| `__main__.py` | Provider bootstrap only. Reads secrets, creates provider, delegates to submodules. |
| `alertmanager.py` | Contact points (Rootly, Slack, oblivion drop sink) and the notification policy route tree. Translates `grafana-alerts/alertmanager.yaml`. |
| `metric_rules/` | Package. Grafana-managed alert rule groups for Prometheus/Mimir metrics. Migrated from `grafana-alerts/cortex-rules/`. |
| `metric_rules/base.py` | Mimir datasource UIDs, two-stage pipeline helper, folder creation, delegates to sub-modules. |
| `metric_rules/eks_general.py` | EKS workload alert rules (replicas, node readiness, crash loops, OOM, jobs, HPA). |
| `metric_rules/linux_host.py` | Linux host alert rules (CPU, memory, disk usage). |
| `metric_rules/apisix_edge.py` | Per-host 5xx rate at the APISIX edge (`apisix_http_status`). Two windows (fast cliff / slow creep) with a minimum-traffic gate. Currently unlabelled → `oblivion` while calibrating. |
| `metric_rules/synthetic_monitoring.py` | MIT Learn probe-failure rules (`probe_success`) for the Next.js origin, the API health endpoint, and the homepage. Imported from hand-made UI rules; lives in the Synthetic Monitoring **plugin's** folder, so it takes no `folder_uid`. |
| `log_rules/` | Package. Grafana-managed alert rule groups for log queries. Migrated from `grafana-alerts/loki-rules/`. |
| `log_rules/base.py` | Loki datasource UIDs, two-stage pipeline helper, folder creation, delegates to sub-modules. |
| `log_rules/cert_manager.py` | cert-manager ACME issuer and DNS challenge alert rules. |
| `log_rules/edxapp.py` | edxapp application log alert rules (500 errors, Redis OOM, credential issues, forum timeouts, SAML). |
| `log_rules/heroku.py` | Heroku application log alert rules (invalid AWS keys, OCW Studio, Keycloak). |
| `log_rules/mit_learn.py` | MIT Learn and MITx Online 5xx error rate alert rules. |
| `log_rules/vault.py` | Vault secret-absent and auth-failure alert rules. |
| `dashboards/` | Package. Grafana dashboards. |
| `dashboards/base.py` | Shared panel-builder helpers, folder creation, delegates to sub-modules. |
| `dashboards/datasources.py` | Mimir/Loki datasource ref constants, importable directly by sub-modules without a circular import through `base.py`. |
| `dashboards/keycloak_olapps_idp_logins.py` | Per-identity-provider login counts (success + failure) for the olapps realm, from Loki via LogQL. |
| `pingdom_checks.py` | Pingdom uptime checks via Pulumi dynamic provider. Runs in the production stack only. |
| `CLAUDE.md` | This file. |

---

## Submodule API

Each top-level module exports a single `create(...)` function. `__main__.py`
calls them in order. No global state is shared between modules; everything
needed is passed as a parameter.

```python
alertmanager.create(grafana_secrets: dict, resource_opts: ResourceOptions)
metric_rules.create(resource_opts: ResourceOptions)
log_rules.create(resource_opts: ResourceOptions)
dashboards.create(resource_opts: ResourceOptions)
pingdom_checks.create(api_token: Input[str], integration_ids: list[int])
```

Within `metric_rules/` and `log_rules/`, each sub-module receives the folder
UID and a pre-bound `rd(expr)` helper from its package `base.py`:

```python
# sub-module signature (metric_rules/* and log_rules/*)
create(folder_uid: Input[str], rd: Callable[[str], list[RuleGroupRuleDataArgs]], resource_opts: ResourceOptions)
```

`metric_rules/synthetic_monitoring.py` is the one exception — it omits
`folder_uid` because its rules must stay in the Synthetic Monitoring plugin's
folder (`grafana-synthetic-monitoring-app`), which Pulumi references but does not
create. The folder UID is half a rule group's import identity, so moving those
rules into "Infrastructure Alerts" would destroy and recreate them, losing their
alert-state history and any live silences.

Within `dashboards/`, each sub-module receives the folder UID, the shared
panel-builder helpers, and a `create_dashboard` helper from `base.py`:

```python
# sub-module signature (dashboards/*)
create(folder_uid, timeseries_panel: Callable[..., dict], bar_gauge_panel: Callable[..., dict], create_dashboard: Callable[..., None], resource_opts: ResourceOptions)
```

---

## Alert rule design (metric_rules/ and log_rules/)

Each Grafana-managed rule uses a two-stage data pipeline:

- **Stage A** — instant PromQL query against the Mimir datasource. The
  expression already encodes the threshold (e.g. `< 1.0`), so it returns a
  non-empty result set only when the condition is met.
- **Stage B** — classic condition: fires when the row count of A is > 0.

`no_data_state="OK"` is set on all rules. EKS rules have cluster-label filters
baked into the PromQL (e.g. `cluster=~".*-(ci|qa)"`). Each Mimir tenant only
holds metrics from its own environment's clusters, so the filter returns no
data on non-matching stacks. `OK` keeps those rules silent rather than showing
a confusing NoData state.

### Adding a new metric alert rule

1. Open the relevant file in `metric_rules/` (`eks_general.py` for EKS
   workload rules, `linux_host.py` for host-level rules, or a new file for
   a new category).
2. Add a new `alerting.RuleGroupRuleArgs(...)` entry to the appropriate
   `alerting.RuleGroup`. If the rule belongs to a new group, create a new
   `alerting.RuleGroup` resource.
3. Use `rd(expr)` to build the two-stage data pipeline from a PromQL expression.
4. Set `for_`, `labels`, `annotations`, and `no_data_state` to match the
   original YAML.

### Adopting a rule that was created in the UI

Rule groups import with `{{ folderUID }}:{{ title }}`, where `title` is the rule
*group* name, not the rule name:

```
pulumi stack select Production
pulumi import grafana:alerting/ruleGroup:RuleGroup \
  <pulumi-resource-name> "<folder-uid>:<rule-group-name>"
```

Import the group before the first `pulumi up` that declares it — an apply
against an undeclared existing group fails as already-exists rather than
adopting it.

Two things to check first:

1. **What else is in the group.** Import adopts the whole group, and any rule in
   it that the code does not declare is deleted on the next apply. Check with
   `GET /api/v1/provisioning/folder/{folderUid}/rule-groups/{group}`.
2. **Pin each rule's `uid`** to the value it already has. Without it the
   provider assigns a fresh one, which orphans the rule's alert-state history,
   breaks live silences, and dead-links the Grafana URL embedded in every past
   Rootly alert.

### Adding a new log alert rule

Same pattern, but open the relevant file in `log_rules/` and use a LogQL
expression. The expression must be metric-producing (use `count_over_time`,
`rate`, `sum`, etc. with a threshold baked in). Bare log stream queries must
be wrapped: `count_over_time({...} |= "pattern" [5m]) > 0`.

---

## Dashboards (dashboards/)

Each Grafana dashboard is its own file, one dashboard per file -- not one
file per product/platform. `metric_rules/` and `log_rules/` group several
related *rules* into one file per category (`eks_general.py`, `heroku.py`);
dashboards don't follow that grouping, because a single growing "keycloak.py"
holding every current and future Keycloak dashboard just recreates the same
"one file for everything in this topic" problem the `dashboards/` package
(vs. a single `dashboards.py`) already exists to avoid.

**Naming**: `<system>_<what-it-shows>.py`, e.g. `keycloak_olapps_idp_logins.py`.
The `<system>` prefix (`keycloak`, in this case) is shared across every
dashboard for that system so they sort and grep together, but each distinct
dashboard -- a different concern, a different realm, whatever varies -- gets
its own file under that same prefix rather than being added to an existing
one. A second Keycloak dashboard about something unrelated (say, session
counts) would be `keycloak_session_counts.py`, not a new function inside
`keycloak_olapps_idp_logins.py`.

### Adding a new dashboard

1. Add `dashboards/<system>_<what_it_shows>.py` with a `_dashboard_json(...)`
   builder and a `create(folder_uid, timeseries_panel, bar_gauge_panel,
   create_dashboard, resource_opts)` function, matching the signature in
   [Submodule API](#submodule-api) above.
2. Use the `timeseries_panel`/`bar_gauge_panel` helpers passed in from
   `base.py` rather than building panel dicts by hand, so styling stays
   consistent across dashboards. They default to the shared Mimir datasource;
   for a Loki-backed panel (LogQL, e.g. `count_over_time`), pass
   `datasource_ref=LOKI_DATASOURCE_REF` (import it from `datasources.py`,
   not `base.py`, to avoid a circular import). Prefer counting straight from
   Loki over deriving a Prometheus counter via an Alloy `stage.metrics`
   block: a `metric.counter` only exists once incremented, so a low-volume
   counter's first-ever appearance is invisible to PromQL's increase() (no
   prior sample to diff against), and it gets evicted and reset after any
   gap longer than `max_idle_duration` -- both silently undercount. LogQL's
   `count_over_time` reads directly from Loki's stored lines at query time:
   no persistent counter state, so neither failure mode applies.
3. Import the new sub-module in `base.py` and call its `create(...)` from
   `base.create(...)`, passing the shared folder UID and helpers.
4. All dashboards in this package currently share one folder (`"Keycloak"`,
   uid `keycloak-dashboards`). If a new dashboard belongs to an unrelated
   system, create a second folder in `base.py` rather than dropping it into
   the Keycloak one.

---

## Pingdom uptime checks (pingdom_checks.py)

Pingdom checks are account-wide resources managed via a Pulumi dynamic provider
that wraps the Pingdom v3 REST API. They run from the **production Pulumi stack
only** to avoid creating duplicate checks across CI/QA/Production stacks.

- **Production checks** (`alert_sensitivity="high"`): 2 probe regions (NA + EU), 1-minute polling
- **Non-production checks** (`alert_sensitivity="low"`): 1 probe region (NA), 1–5 minute polling

Each `_PingdomCheck` resource calls `POST /checks` on create, `PUT /checks/{id}`
on update, and `DELETE /checks/{id}` on destroy. The Pingdom check ID is stored
as the Pulumi resource ID in state, so `pulumi refresh` detects drift if a check
is manually changed or deleted in the Pingdom UI — **in principle**. In practice,
the 39 checks currently live in the production Pingdom account are **not**
tracked in Pulumi state at all; see
[docs/adr/0010-pingdom-checks-unmanaged-in-pulumi-state.md](../../../../docs/adr/0010-pingdom-checks-unmanaged-in-pulumi-state.md)
for why (`pulumi import` cannot adopt Python dynamic-provider resources) and
what the options are for fixing it properly.

Because of that gap, `pingdom_checks.create()` skips registering any of the 39
checks (logging a warning instead) unless `pulumi config set
allow_pingdom_apply true` has been set on the stack — a plain `pulumi up`
would otherwise try to create all 39 checks again, producing real duplicates
in Pingdom. This is a skip, not a hard failure, so the rest of the stack
(rule groups, contact points, notification policy) stays fully manageable
without opting in. Read the ADR above before setting that flag.

### Adding a new Pingdom check

Add an `_SMCheck(...)` entry to the `_CHECKS` list in `pingdom_checks.py`. The
`resource_name` must be unique and follow the pattern `<service>-<env>-http`.
Set `alert_sensitivity="high"` for production services and `"low"` for
QA/RC/staging. Set `paused=True` if the target is known to be down at creation
time.

Until ADR 0010 is resolved, creating it through Pulumi is unreliable: `create()`
has repeatedly reported failure while the check was nonetheless created
correctly in Pingdom (see the ADR's Issue 2). After running `pulumi up
--target <urn>` for the new check, always verify directly against the Pingdom
API (`GET /api/3.1/checks`) that exactly one new check appeared before trusting
Pulumi's own reported result.

---

## Notification routing

The notification policy in `alertmanager.py` mirrors the original
`grafana-alerts/alertmanager.yaml` route tree:

1. `channel=notifications-ocw-misc` → Slack by severity (warning/critical),
   anything else with that label is silenced.
2. `alertname=~Kube.*` → silenced (built-in k8s noise, not actionable).
3. `severity=warning` → Rootly.
4. `severity=critical` → Rootly.
5. Default (catch-all) → `oblivion` (empty contact point, acts as drop sink).

OpsGenie is no longer active. All actionable alerts route to Rootly.

**Rule 5 is load-bearing, not just a fallback.** A rule carrying no `severity`
label is still evaluated and still recorded in `grafanacloud-alert-state-history`
— it is simply delivered nowhere. `metric_rules/apisix_edge.py` uses that
deliberately, to calibrate new thresholds against real firing data at zero paging
risk; promoting such a rule means adding a `severity` label and nothing else.

That "nothing else" holds only if the rule's resource-identifying label is
already in `NotificationPolicy.group_bies`. A rule aggregating `sum by (X)`
carries `X` as its only such label, and if `X` is missing from that list every
firing instance collapses into one notification group per rule — the bundling
the list exists to prevent. `matched_host` was added there for
`apisix_edge.py`; when adding a rule that groups by a new label, add it too.

The same mechanism silently swallows rules that lost their label *by accident*:
`HTTPRequestDurationTooHighAvg` fired 1,168 times in 30 days into `oblivion`
before anyone noticed. When adding a rule, be explicit about which of the two
you mean.

---

## Secrets reference

Required keys per secrets file:

```yaml
# All stacks:
grafana_url: https://<stack>.grafana.net
grafana_api_token: <service-account-token>
rootly_bearer_token: <rootly-webhook-bearer-token>
slack_notifications_ocw_misc_api_url: <slack-webhook-url>

# Production only (Pingdom checks run from production stack only):
pingdom_api_token: <pingdom-api-token>
pingdom_integration_ids: [<integration-id>, ...]  # Pingdom integration IDs for alert routing
```

---

## Phase status (as of 2026-08-05)

- **Phase 5** — Done. The legacy Grafana Concourse pipelines have been deleted:
  `src/ol_concourse/pipelines/infrastructure/grafana_cloud/` (grizzly dashboard
  sync + cortextool cortex/loki rule sync) and the older hand-written YAML
  pipelines `pipelines/infrastructure/{grizzly,cortextool}/`. Alert rules and
  Alertmanager config are now managed solely by this Pulumi program, deployed
  via the `grafana-alerting` simple_pulumi pipeline. Note that the hourly
  CI → QA → Production sync of the `mitodl/grafana-dashboards` repo went away
  with it and has no Pulumi replacement.
- **Phase 6** — Rename SNS topics `OpsGenie_Critical_Notifications` /
  `OpsGenie_Warning_Notifications` to reflect Rootly (cosmetic, low priority).
  Note: renaming an SNS topic changes its ARN and requires updating all
  CloudWatch alarm subscriptions that reference it.
