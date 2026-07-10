"""Grafana metric alert rule groups (Prometheus/Mimir).

Migrated from grafana-alerts/cortex-rules/ (previously synced via cortextool).

Two-stage evaluation pipeline per rule
---------------------------------------
Grafana-managed alert rules cannot use a bare PromQL expression as a condition
the way the Mimir ruler API can. Instead each rule needs:

  Stage A — instant PromQL query against the per-environment Mimir datasource.
             The PromQL already contains the threshold (e.g. "< 1.0"), so it
             returns a non-empty result set only when the condition is met.
  Stage B — classic condition: fires when the count of rows returned by A > 0.

no_data_state="OK"
-------------------
Every EKS rule has a cluster label selector baked into its PromQL expression
(e.g. cluster=~".*-(ci|qa)"). Each Grafana Cloud stack queries its own Mimir
tenant, which only has metrics from the matching environment's clusters. When
the selector returns no data (e.g. the production filter on the CI stack),
no_data_state="OK" keeps the rule silent instead of surfacing a NoData alert.

Sub-modules
-----------
  eks_general  — Source: grafana-alerts/cortex-rules/eks_general.yaml
  linux_host   — Source: grafana-alerts/cortex-rules/linux-host.yaml
"""

import json

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.metric_rules import (
    eks_general,
    linux_host,
)

# Every Grafana Cloud stack provisions its own Mimir datasource with this same
# generic UID. The per-stack slug (e.g. grafanacloud-mitolci-prom) is only the
# datasource *name*; referencing it as a UID fails with "data source not found".
_MIMIR_DATASOURCE_UID = "grafanacloud-prom"


def _rule_data(expr: str, mimir_uid: str) -> list[alerting.RuleGroupRuleDataArgs]:
    """Build the two-stage data pipeline for a single alert rule."""
    return [
        # Stage A: run the PromQL expression against Mimir.
        alerting.RuleGroupRuleDataArgs(
            ref_id="A",
            datasource_uid=mimir_uid,
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "datasource": {"type": "prometheus", "uid": mimir_uid},
                    "expr": expr,
                    "instant": True,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
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
    """Create all Grafana metric alert rule groups."""
    mimir_uid = _MIMIR_DATASOURCE_UID

    alerts_folder = Folder(
        "infrastructure-alerts-folder",
        title="Infrastructure Alerts",
        uid="infrastructure-alerts",
        opts=resource_opts,
    )

    def rd(expr: str) -> list[alerting.RuleGroupRuleDataArgs]:
        return _rule_data(expr, mimir_uid)

    eks_general.create(alerts_folder.uid, rd, resource_opts)
    linux_host.create(alerts_folder.uid, rd, resource_opts)
