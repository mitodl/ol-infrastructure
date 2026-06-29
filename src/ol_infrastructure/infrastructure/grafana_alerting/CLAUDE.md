# grafana_alerting — architecture reference

This Pulumi program manages all Grafana Cloud alerting configuration for
MIT Open Learning. It replaces two legacy systems:
- **Pingdom** → Grafana Synthetic Monitoring (SM) checks
- **grafana-alerts repo + cortextool** → Grafana-managed alert rules and Alertmanager config

---

## Grafana Cloud stacks

There are three separate Grafana Cloud stacks, one per environment:

| Stack | Secrets file | Mimir datasource UID |
|---|---|---|
| CI | `src/bridge/secrets/grafana_cloud/api.ci.yaml` | `grafanacloud-mitolci-prom` |
| QA | `src/bridge/secrets/grafana_cloud/api.qa.yaml` | `grafanacloud-mitolqa-prom` |
| Production | `src/bridge/secrets/grafana_cloud/api.production.yaml` | `grafanacloud-mitolproduction-prom` |

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
| `metric_rules.py` | Grafana-managed alert rule groups. Migrated from `grafana-alerts/cortex-rules/`. |
| `sm_checks.py` | Synthetic Monitoring uptime checks. Runs in the production stack only. Replaces Pingdom. |
| `CLAUDE.md` | This file. |

---

## Submodule API

Each submodule exports a single `create(...)` function. `__main__.py` calls
them in order. No global state is shared between modules; everything needed
is passed as a parameter.

```python
alertmanager.create(grafana_secrets: dict, resource_opts: ResourceOptions)
metric_rules.create(stack_info: StackInfo, resource_opts: ResourceOptions)
sm_checks.create(invoke_opts: InvokeOptions, resource_opts: ResourceOptions)
```

---

## Alert rule design (metric_rules.py)

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

1. Open `metric_rules.py`.
2. Add a new `alerting.RuleGroupRuleArgs(...)` entry to the appropriate
   `alerting.RuleGroup`. If the rule belongs to a new group, create a new
   `alerting.RuleGroup` resource.
3. Use `rd(expr)` to build the two-stage data pipeline from a PromQL expression.
4. Set `for_`, `labels`, `annotations`, and `no_data_state` to match the
   original YAML.

---

## Synthetic Monitoring checks (sm_checks.py)

SM checks run in the **production Grafana stack only**, regardless of which
URL they target (QA, RC, production, etc.). This avoids fragmenting probe
config and alert sensitivity settings across three stacks.

Probes used: Atlanta, Frankfurt, Singapore, Sydney. IDs are resolved by name at
deploy time (`syntheticmonitoring.get_probes`) so numeric IDs are never
hardcoded.

### Adding a new SM check

Add an `_SMCheck(...)` entry to the `_CHECKS` list in `sm_checks.py`. The
`resource_name` must be unique and follow the pattern
`<service>-<env>-http`.

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

---

## Secrets reference

Required keys per secrets file (non-production files omit the SM fields):

```yaml
grafana_url: https://<stack>.grafana.net
grafana_api_token: <service-account-token>
rootly_bearer_token: <rootly-webhook-bearer-token>
slack_notifications_ocw_misc_api_url: <slack-webhook-url>

# Production only:
grafana_sm_url: https://synthetic-monitoring-api.grafana.net
grafana_sm_access_token: <sm-access-token>
```

---

## Pending phases (as of 2026-06-29)

- **Phase 4** — Loki log-based alert rules (not yet started).
- **Phase 5** — Remove cortextool sync jobs from the Grafana Cloud Concourse
  pipeline (`src/ol_concourse/pipelines/infrastructure/grafana_cloud/pipeline.py`)
  once Pulumi rules are verified in production.
- **Phase 6** — Rename SNS topics `OpsGenie_Critical_Notifications` /
  `OpsGenie_Warning_Notifications` to reflect Rootly (cosmetic, low priority).
