# grafana_alerting — architecture reference

This Pulumi program manages all Grafana Cloud alerting configuration for
MIT Open Learning. It replaces two legacy systems:
- **Pingdom** → Grafana Synthetic Monitoring (SM) checks
- **grafana-alerts repo + cortextool** → Grafana-managed alert rules and Alertmanager config

---

## Grafana Cloud stacks

There are three separate Grafana Cloud stacks, one per environment:

| Stack | Secrets file | Mimir (metrics) UID | Loki (logs) UID |
|---|---|---|---|
| CI | `src/bridge/secrets/grafana_cloud/api.ci.yaml` | `grafanacloud-mitolci-prom` | `grafanacloud-mitolci-logs` |
| QA | `src/bridge/secrets/grafana_cloud/api.qa.yaml` | `grafanacloud-mitolqa-prom` | `grafanacloud-mitolqa-logs` |
| Production | `src/bridge/secrets/grafana_cloud/api.production.yaml` | `grafanacloud-mitolproduction-prom` | `grafanacloud-mitolproduction-logs` |

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
| `metric_rules/__init__.py` | Mimir datasource UIDs, two-stage pipeline helper, folder creation, delegates to sub-modules. |
| `metric_rules/eks_general.py` | EKS workload alert rules (replicas, node readiness, crash loops, OOM, jobs, HPA). |
| `metric_rules/linux_host.py` | Linux host alert rules (CPU, memory, disk usage). |
| `log_rules/` | Package. Grafana-managed alert rule groups for log queries. Migrated from `grafana-alerts/loki-rules/`. |
| `log_rules/__init__.py` | Loki datasource UIDs, two-stage pipeline helper, folder creation, delegates to sub-modules. |
| `log_rules/cert_manager.py` | cert-manager ACME issuer and DNS challenge alert rules. |
| `log_rules/edxapp.py` | edxapp application log alert rules (500 errors, Redis OOM, credential issues, forum timeouts, SAML). |
| `log_rules/heroku.py` | Heroku application log alert rules (invalid AWS keys, Bootcamps SAML, OCW Studio, Keycloak). |
| `log_rules/mit_learn.py` | MIT Learn and MITx Online 5xx error rate alert rules. |
| `log_rules/vault.py` | Vault secret-absent and auth-failure alert rules. |
| `sm_checks.py` | Synthetic Monitoring uptime checks. Runs in the production stack only. Replaces Pingdom. |
| `CLAUDE.md` | This file. |

---

## Submodule API

Each top-level module exports a single `create(...)` function. `__main__.py`
calls them in order. No global state is shared between modules; everything
needed is passed as a parameter.

```python
alertmanager.create(grafana_secrets: dict, resource_opts: ResourceOptions)
metric_rules.create(stack_info: StackInfo, resource_opts: ResourceOptions)
log_rules.create(stack_info: StackInfo, resource_opts: ResourceOptions)
sm_checks.create(invoke_opts: InvokeOptions, resource_opts: ResourceOptions)
```

Within `metric_rules/` and `log_rules/`, each sub-module receives the folder
UID and a pre-bound `rd(expr)` helper from its package `__init__.py`:

```python
# sub-module signature (metric_rules/* and log_rules/*)
create(folder_uid: Input[str], rd: Callable[[str], list[RuleGroupRuleDataArgs]], resource_opts: ResourceOptions)
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

### Adding a new log alert rule

Same pattern, but open the relevant file in `log_rules/` and use a LogQL
expression. The expression must be metric-producing (use `count_over_time`,
`rate`, `sum`, etc. with a threshold baked in). Bare log stream queries must
be wrapped: `count_over_time({...} |= "pattern" [5m]) > 0`.

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

## Pending phases (as of 2026-07-02)

- **Phase 5** — Remove cortextool sync jobs (both cortex and loki) from the
  Grafana Cloud Concourse pipeline
  (`src/ol_concourse/pipelines/infrastructure/grafana_cloud/pipeline.py`)
  once Pulumi rules (Phases 3 and 4) are verified in production.
- **Phase 6** — Rename SNS topics `OpsGenie_Critical_Notifications` /
  `OpsGenie_Warning_Notifications` to reflect Rootly (cosmetic, low priority).
  Note: renaming an SNS topic changes its ARN and requires updating all
  CloudWatch alarm subscriptions that reference it.
