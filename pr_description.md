## Migrate Grafana Alerting + Uptime Monitoring to Pulumi (Phases 1–4)

Closes #4828 (partial — phases 1–4 of 6)

---

### Summary

This PR migrates the first four phases of #4828 — replacing Pingdom (via a Pulumi dynamic provider), retiring the `grafana-alerts` repo's alertmanager config, and moving all Cortex/Mimir metric alert rules and Loki log-based alert rules into Pulumi-managed Grafana resources.

All changes live in a new Pulumi program at `src/ol_infrastructure/infrastructure/grafana_alerting/` with three stacks (CI, QA, Production), each managing its own Grafana Cloud instance.

---

### Phase 1 — Pingdom uptime checks (via Pulumi dynamic provider)

**39 uptime checks** managed through a Pulumi dynamic provider that wraps the Pingdom v3 REST API. Each check is a first-class Pulumi resource — `pulumi up` creates, updates, and deletes checks in Pingdom, and `pulumi refresh` detects drift if checks are manually changed in the Pingdom UI.

Grafana Synthetic Monitoring was evaluated as the initial approach but ruled out due to cost: at 4 probe regions × 1-minute polling, SM executions were projected at ~$3,200/month. Pingdom is significantly cheaper for equivalent uptime coverage.

- **Production checks** (20): 2 probe regions (NA + EU), 1-minute polling
- **Non-production checks** (19, QA/RC/staging): 1 probe region (NA), 1–5 minute polling
- **Production stack only** — Pingdom is account-wide; creating from one stack avoids duplicate checks

Three checks were intentionally skipped (paused/dead):

- `ChrisTestNukeMe` — test check, not a real service
- `xPro preview production` (preview.xpro.mit.edu) — dead since 2022
- `MITx production CMS` (studio.mitx.mit.edu) — paused 2023, superseded by "MITx Production Studio"

Three xPro production checks (`xpro.mit.edu`, `courses.xpro.mit.edu`, `studio.xpro.mit.edu`) are created with `paused=True` — they were DOWN at migration time and will alert immediately if enabled without investigation.

---

### Phase 2 — Alertmanager contact points + notification policy

Migrates `grafana-alerts/alertmanager.yaml` into Pulumi-managed `grafana.alerting` resources, replacing placeholder-substitution via Concourse.

**Contact points created:**

| Name | Destination |
|------|-------------|
| `oblivion` | Empty drop sink (silences matched routes) |
| `rootly` | Rootly webhook (all actionable warning + critical alerts) |
| `slack-notifications-ocw-misc-warning` | #notifications-ocw-misc, warning severity |
| `slack-notifications-ocw-misc-critical` | #notifications-ocw-misc, critical severity |

**Notification policy** mirrors the original route tree:

1. `channel=notifications-ocw-misc` → Slack by severity; unmatched alerts with this label are silenced
2. `alertname=~Kube.*` → silenced (built-in k8s noise, not actionable)
3. `severity=warning` → Rootly
4. `severity=critical` → Rootly
5. Default → `oblivion`

---

### Phase 3 — Metric alert rules (replaces cortextool sync)

Migrates `grafana-alerts/cortex-rules/eks_general.yaml` and `grafana-alerts/cortex-rules/linux-host.yaml` into Pulumi-managed `grafana.alerting.RuleGroup` resources. All rule groups live in a new Grafana folder: **Infrastructure Alerts**.

| Pulumi resource | Source file | Rules | Eval interval |
|-----------------|-------------|-------|---------------|
| `eks-general` | `cortex-rules/eks_general.yaml` | 20 | 1m |
| `linux-host-cpu-usage` | `cortex-rules/linux-host.yaml` | 1 | 1h |
| `linux-host-memory-usage` | `cortex-rules/linux-host.yaml` | 1 | 30m |
| `linux-host-disk-usage` | `cortex-rules/linux-host.yaml` | 2 | 10m |

---

### Phase 4 — Log-based alert rules (replaces cortextool loki sync)

Migrates all five files from `grafana-alerts/loki-rules/` into Pulumi-managed `grafana.alerting.RuleGroup` resources. All rule groups live in a new Grafana folder: **Log Alerts**.

| Sub-module | Source file | Rule groups | Rules |
|------------|-------------|-------------|-------|
| `log_rules/cert_manager.py` | `loki-rules/cert-manager.yaml` | 2 | 4 |
| `log_rules/edxapp.py` | `loki-rules/edxapp-logs.yaml` | 3 | 9 |
| `log_rules/heroku.py` | `loki-rules/heroku-logs.yaml` | 4 | 11 |
| `log_rules/mit_learn.py` | `loki-rules/mit-learn.yaml` | 1 | 2 |
| `log_rules/vault.py` | `loki-rules/vault.yaml` | 1 | 2 |

Two small adjustments from the original YAML to fit the Grafana-managed two-stage alert pipeline:

- **Keycloak rules** — bare log stream queries (`{application="keycloak"} |= "..."`) wrapped in `count_over_time([5m]) > 0` to make them metric-producing, so Stage B can apply a threshold correctly.
- **OCW Studio content-sync password rules** — `> 0` appended to expressions that had no threshold, which would otherwise cause Stage B to fire even when the count is zero.

---

### Alert evaluation pipeline (Phases 3 & 4)

Each rule uses a two-stage pipeline:

- **Stage A** — instant query against Mimir (metric rules) or Loki (log rules). The expression encodes the threshold so it returns no rows when the condition is not met.
- **Stage B** — classic condition: fires when A returns any results (count > 0).

`no_data_state="OK"` is set on all rules. Each Grafana Cloud stack has its own Mimir and Loki tenant scoped to that environment, so cluster- or environment-filtered rules return no data on non-matching stacks — `OK` keeps those silent rather than surfacing NoData alerts.

---

### Code structure

```
grafana_alerting/
├── __main__.py            # provider bootstrap only
├── alertmanager.py        # Phase 2 — contact points + notification policy
├── metric_rules/          # Phase 3 — Prometheus/Mimir metric alert rules
│   ├── base.py            # datasource UIDs, pipeline helper, folder creation
│   ├── eks_general.py     # EKS workload rules
│   └── linux_host.py      # Linux host CPU/memory/disk rules
├── log_rules/             # Phase 4 — log-based alert rules
│   ├── base.py            # Loki datasource UIDs, pipeline helper, folder creation
│   ├── cert_manager.py
│   ├── edxapp.py
│   ├── heroku.py
│   ├── mit_learn.py
│   └── vault.py
├── pingdom_checks.py      # Phase 1 — Pingdom uptime checks (dynamic provider)
└── CLAUDE.md              # architecture reference for future contributors
```

---

### What's NOT in this PR (remaining phases)

| Phase | Description |
|-------|-------------|
| 5 | Remove cortex and loki sync jobs from the Grafana Cloud Concourse pipeline — follow-up after production verification |
| 6 | Rename SNS topics from `OpsGenie_*` to reflect Rootly (cosmetic, low priority) |

---

### Deploy checklist

- [ ] Fill in `REPLACE_ME` values in all three secrets files and SOPS-encrypt them
- [ ] Add `pingdom_api_token` and `pingdom_integration_ids` to `api.production.yaml` and SOPS-encrypt
- [ ] `pulumi up` on the Production stack — verify Pingdom checks appear in app.pingdom.com
- [ ] Enable the three paused xPro checks after investigating their downtime
- [ ] `pulumi up` on the Production stack — verify metric rule groups appear in Grafana Alerting → **Infrastructure Alerts** folder
- [ ] `pulumi up` on the Production stack — verify log rule groups appear in Grafana Alerting → **Log Alerts** folder
- [ ] Confirm contact points and notification policy match the existing Alertmanager config
- [ ] After verifying production, `pulumi up` on CI and QA stacks
- [ ] Once Pulumi rules are confirmed working in production, remove cortextool sync from Concourse (Phase 5)
