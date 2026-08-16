# Orphaned cortextool-era ruler snapshots

Captured 2026-08-16 via the Grafana Cloud ruler and Alertmanager APIs, immediately
before deleting the unmanaged `eks`, `linux-host` and Loki namespaces. That deletion
is step W1 of the Grafana alerting remediation, whose plan is not committed to this
repo — this file is written to stand on its own, so nothing below depends on reading
it.

These namespaces are **not** in Pulumi. Nothing else holds a copy. This directory is
the only restore source if a deletion turns out to be wrong.

## What was live at capture time

| Stack | Mimir namespaces | Loki namespaces | Legacy Alertmanager |
|---|---|---|---|
| production | `asserts` 71grp/630, `integrations-kubernetes` 25grp/124, **`eks` 1grp/6**, **`linux-host` 3grp/4** | **5xx-errors 2, cert-manager 4, edxapp-logs 9, heroku-logs 12, vault 2** | route tree with `rootly` receiver |
| qa | `asserts` 68grp/614, `integrations-kubernetes` 25grp/124, **`eks` 1grp/6**, **`linux-host` 3grp/4** | identical 29 rules | byte-identical route tree |
| ci | `asserts` 2grp/2, no `integrations-kubernetes`, **`eks` 1grp/20**, **`linux-host` 3grp/4** | identical 29 rules | byte-identical route tree |

Bold = to be deleted. `asserts` and `integrations-kubernetes` are Grafana Cloud's own
vendor-managed namespaces and must never be touched.

## Files

- `production-mimir.json` — full bodies of prod `eks` + `linux-host`
- `production-loki.json` — full bodies of all five prod Loki namespaces
- `production-alertmanager.json` — legacy Cloud Alertmanager config, with **two**
  credentials replaced by placeholders (see below)
- `ci-mimir.json` — CI's `eks` (the modern 20-rule set) + `linux-host`
- `qa.md` — why QA needs no separate body dump

### Redacted credentials and where to recover them

A restore from `production-alertmanager.json` must substitute both, or delivery
comes back broken — Rootly silently, Slack visibly. Both live in the same
SOPS-encrypted per-stack secrets file, `src/bridge/secrets/grafana_cloud/api.<env>.yaml`,
under the keys the Pulumi program already reads in
`src/ol_infrastructure/infrastructure/grafana_alerting/alertmanager.py`:

| Placeholder in the snapshot | Secrets key | Used by |
|---|---|---|
| `REDACTED-ROOTLY-BEARER` | `rootly_bearer_token` | the `rootly` webhook receiver |
| `REDACTED-SLACK-WEBHOOK` (appears twice) | `slack_notifications_ocw_misc_api_url` | both `slack-notifications-ocw-misc-*` receivers |

```
sops -d src/bridge/secrets/grafana_cloud/api.production.yaml | yq -r '.rootly_bearer_token'
```

The Rootly bearer is additionally recoverable from Rootly itself — it is that
stack's alert-source token.

## Deletion executed 2026-08-16

Run in the required order per stack — the seven rule namespaces first, the legacy
Alertmanager config last, so no rule could fire into a default receiver during the
gap. Production, then QA, then CI.

State after, verified against each stack's live API:

| Stack | Mimir ruler | Loki ruler | Legacy Alertmanager | Grafana-managed rules |
|---|---|---|---|---|
| production | `asserts`, `integrations-kubernetes` | none (404) | `{}` — no `rootly` receiver | 24 groups / 72 rules |
| qa | `asserts`, `integrations-kubernetes` | none (404) | `{}` — no `rootly` receiver | 17 groups / 64 rules |
| ci | `asserts` | none (404) | 404, storage object gone | 16 groups / 56 rules |

Only the vendor-managed namespaces remain, and the Grafana-managed (Pulumi) rules
are untouched on all three — the double-delivery path is gone.

Still outstanding: the third verification in the task — that over the following 24h
no Rootly alert arrives carrying an Explore-deeplink `external_url`, which is the
ruler-generated signature. Nothing should now be able to produce one.

Nothing was left uncovered. The one orphaned rule without a Pulumi equivalent
belonged to a retired application; see the section above.

## Verified equivalences at capture time

- QA `eks` and `linux-host` are **byte-identical** to production's. Restore from
  `production-mimir.json`.
- QA and CI Loki namespaces are **byte-identical** to production's (same 29 rules,
  same groups, same intervals, same exprs, same labels). Restore from
  `production-loki.json`. Note these rules hardcode
  `cluster="applications-production"` / `environment=~".*production"`, which is why
  they are dead weight on the QA and CI tenants — each tenant only holds its own
  clusters.
- QA and CI legacy Alertmanager route trees are byte-identical to production's,
  each with its own `rootly` webhook receiver. Restore from
  `production-alertmanager.json` (the bearer credential is the per-stack Rootly
  alert-source token — recoverable from Rootly, not from here).

## Correction to the plan's count — and why it changed nothing

W1 asserts that all 10 orphaned metric rules and all 28 orphaned Loki rules have
live Pulumi equivalents. The metric half checks out — all 10 names appear in
`metric_rules/eks_general.py` and `metric_rules/linux_host.py`. The Loki count is
wrong: the orphaned ruler held **29** rules, not 28. The extra one is

  heroku-logs / bootcamps / **BootcampsSAMLIntegrationErrorProd**

and it had no equivalent anywhere in `log_rules/`, nor anywhere else in the repo —
`bootcamp` and the literal `Unable to refresh local metadata` both matched nothing.

It was **deliberately not ported**: the Bootcamps application has been fully
retired, so the alert had nothing left to watch. `bootcamp-ecommerce` stopped
shipping logs when the app went away, which is also why the rule could never have
fired again regardless. Deleting it alongside the rest of `heroku-logs` is the
correct outcome, not a coverage loss.

The count is recorded here anyway because the *method* matters: the plan's
"everything has an equivalent" claim was a hypothesis, and diffing the two rule-name
sets rather than trusting the prose is what surfaced the discrepancy. Had the
service still been live, this would have been a real hole.

## CI is not a third identical stack

CI's `eks` holds 20 rules, not 6, and they are the *current, correct* rule set with
correct environment filters and lowercase severities — the same content Pulumi
deploys. CI's `linux-host` `CPUUsageWarning` is likewise the fixed form
(`sum by (cluster, instance)`), not production's dead `host="$instance"` version.
CI still double-delivers and is still in scope for deletion, but none of the
"stale and provably wrong" justification applies there.
