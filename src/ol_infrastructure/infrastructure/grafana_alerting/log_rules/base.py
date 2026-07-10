"""Grafana log-based alert rule groups (Loki/LogQL).

Migrated from grafana-alerts/loki-rules/ (previously synced via cortextool).

Two-stage evaluation pipeline per rule
---------------------------------------
Same pattern as metric_rules, but Stage A queries the Loki datasource with a
LogQL metric expression instead of PromQL:

  Stage A — LogQL metric query against the per-environment Loki datasource.
             Must be metric-producing (count_over_time, rate, etc.) with the
             threshold baked in so it returns no rows when the condition is
             not met.
  Stage B — classic condition: fires when count of A > 0.

Differences from the original YAML
------------------------------------
- Bare log stream queries (no aggregation) are wrapped in
  count_over_time([5m]) > 0 to make them metric-producing. Affected rules:
  KeycloakInternalError, KeycloakSAMLAssertionDecryptError, KeycloakServerError.
- Expressions without a threshold have > 0 appended so Stage A returns no rows
  when the count is zero. Affected rules:
  OCWStudioContentSyncInvalidPassword{NonProd,Prod}.
- no_data_state="OK" is set on all rules. Each Grafana Cloud stack has its own
  Loki tenant; rules filtered to production clusters return no data on CI/QA
  stacks and vice versa. OK keeps those rules silent rather than surfacing
  NoData alerts.

Sub-modules
-----------
  cert_manager — Source: grafana-alerts/loki-rules/cert-manager.yaml
  edxapp       — Source: grafana-alerts/loki-rules/edxapp-logs.yaml
  heroku       — Source: grafana-alerts/loki-rules/heroku-logs.yaml
  mit_learn    — Source: grafana-alerts/loki-rules/mit-learn.yaml
  vault        — Source: grafana-alerts/loki-rules/vault.yaml
"""

import json

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.log_rules import (
    cert_manager,
    edxapp,
    heroku,
    mit_learn,
    vault,
)

# Every Grafana Cloud stack provisions its own Loki datasource with this same
# generic UID. The per-stack slug (e.g. grafanacloud-mitolci-logs) is only the
# datasource *name*; referencing it as a UID fails with "data source not found".
_LOKI_DATASOURCE_UID = "grafanacloud-logs"


def _rule_data(expr: str, loki_uid: str) -> list[alerting.RuleGroupRuleDataArgs]:
    """Build the two-stage data pipeline for a single log alert rule."""
    return [
        # Stage A: run the LogQL metric expression against Loki.
        alerting.RuleGroupRuleDataArgs(
            ref_id="A",
            datasource_uid=loki_uid,
            # Grafana copies queryType from the model up to the data envelope;
            # omitting it here shows as a perpetual diff on every preview.
            query_type="range",
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "datasource": {"type": "loki", "uid": loki_uid},
                    "expr": expr,
                    "queryType": "range",
                    "refId": "A",
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                }
            ),
        ),
        # Stage B: fire when A returns any rows (count > 0).
        alerting.RuleGroupRuleDataArgs(
            ref_id="B",
            datasource_uid="-100",  # "-100" is Grafana's internal UID for expressions
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "query": {"params": ["A"]},
                            "reducer": {"type": "count", "params": []},
                            "type": "query",
                        }
                    ],
                    "datasource": {"type": "__expr__", "uid": "-100"},
                    "refId": "B",
                    "type": "classic_conditions",
                }
            ),
        ),
    ]


def create(resource_opts: ResourceOptions) -> None:
    """Create all Grafana log-based alert rule groups."""
    loki_uid = _LOKI_DATASOURCE_UID

    log_alerts_folder = Folder(
        "log-alerts-folder",
        title="Log Alerts",
        uid="log-alerts",
        opts=resource_opts,
    )

    def rd(expr: str) -> list[alerting.RuleGroupRuleDataArgs]:
        return _rule_data(expr, loki_uid)

    cert_manager.create(log_alerts_folder.uid, rd, resource_opts)
    edxapp.create(log_alerts_folder.uid, rd, resource_opts)
    heroku.create(log_alerts_folder.uid, rd, resource_opts)
    mit_learn.create(log_alerts_folder.uid, rd, resource_opts)
    vault.create(log_alerts_folder.uid, rd, resource_opts)
