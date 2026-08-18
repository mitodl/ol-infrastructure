"""Grafana metric alert rule groups (Prometheus/Mimir).

Migrated from grafana-alerts/cortex-rules/ (previously synced via cortextool).

Three-stage evaluation pipeline per rule
-----------------------------------------
Grafana-managed alert rules cannot use a bare PromQL expression as a condition
the way the Mimir ruler API can. Instead each rule needs:

  Stage A — instant PromQL query against the per-environment Mimir datasource.
             The PromQL already contains the threshold (e.g. "< 1.0"), so it
             returns a non-empty result set only when the condition is met.
  Stage B — reduce: passes each series returned by A through unchanged
             (reducer "last" on a single instant value is a no-op numerically).
  Stage C — threshold: fires per-series when B's value is > 0.

Stages B and C use "reduce"/"threshold" expressions rather than a single
"classic_condition", specifically so each matching series keeps its own
labels (cluster, namespace, pod, container, ...) on the resulting alert
instance. A classic condition instead collapses every matching series into
one alertname-only instance with no labels at all -- see
https://grafana.com/docs/grafana/latest/alerting/alerting-rules/templates/examples/
("You cannot use $labels ... if you are using classic conditions"). This
silently broke every `{{ $labels.x }}` reference in every rule's annotations
until fixed (all alerts fired with "[no value]" in place of the real labels).

no_data_state="OK"
-------------------
Every EKS rule has a cluster label selector baked into its PromQL expression
(e.g. cluster=~".*-(ci|qa)"). Each Grafana Cloud stack queries its own Mimir
tenant, which only has metrics from the matching environment's clusters. When
the selector returns no data (e.g. the production filter on the CI stack),
no_data_state="OK" keeps the rule silent instead of surfacing a NoData alert.

exec_err_state
---------------
Distinct from no_data_state: this fires on a query-evaluation *failure* (e.g.
a transient datasource blip), not on an empty result set. Left unset, it
silently defaults to Grafana's "Alerting" -- a datasource hiccup then pages
like a real incident on every affected rule. Every rule here sets it
explicitly: "OK" for warning-tier rules (going silent during an error is an
acceptable trade), "KeepLast" for critical-tier rules (holds the rule's last
known state through the error instead of either paging on a blip or going
silent through a genuine ongoing incident). See eks_general.py's docstring
for the incident that prompted this.

Sub-modules
-----------
  eks_general  — Source: grafana-alerts/cortex-rules/eks_general.yaml
  linux_host   — Source: grafana-alerts/cortex-rules/linux-host.yaml
  apisix_edge  — New in 2026-08. Per-host 5xx rate at the APISIX edge.
  dagster_pgbouncer
               — New in 2026-08. Dagster's PgBouncer pool: aggregate connection
                 headroom against the max_db_connections cap, queued clients,
                 connection turnover, and exporter health. Depends on the
                 pgbouncer_exporter sidecar added to the dagster stack in #5426.
                 Pairs with log_rules/dagster_database.py, which covers the
                 client side of the same relationship -- nothing here can see a
                 client that fails before it becomes a connection.
  synthetic_monitoring
               — Imported 2026-08 from three hand-made UI rules. Unlike the
                 others it takes no folder_uid: its rules live in the Synthetic
                 Monitoring plugin's folder, not the one created here.
"""

import json

from pulumi import ResourceOptions
from pulumiverse_grafana import alerting
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.metric_rules import (
    apisix_edge,
    dagster_pgbouncer,
    eks_general,
    linux_host,
    synthetic_monitoring,
)

# Every Grafana Cloud stack provisions its own Mimir datasource with this same
# generic UID. The per-stack slug (e.g. grafanacloud-mitolci-prom) is only the
# datasource *name*; referencing it as a UID fails with "data source not found".
_MIMIR_DATASOURCE_UID = "grafanacloud-prom"


def _rule_data(expr: str, mimir_uid: str) -> list[alerting.RuleGroupRuleDataArgs]:
    """Build the three-stage data pipeline for a single alert rule."""
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
        # Stage B: reduce (per series, labels preserved -- unlike a classic
        # condition). "last" is a no-op on an instant query's single point.
        alerting.RuleGroupRuleDataArgs(
            ref_id="B",
            datasource_uid="-100",  # "-100" is Grafana's internal UID for expressions
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "type": "reduce",
                    "datasource": {"type": "__expr__", "uid": "-100"},
                    "expression": "A",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": []},
                            "operator": {"type": "and"},
                            "query": {"params": ["A"]},
                            "reducer": {"type": "last", "params": []},
                            "type": "query",
                        }
                    ],
                    "reducer": "last",
                    "refId": "B",
                }
            ),
        ),
        # Stage C: threshold, per series -- fires when B's value is > 0.
        alerting.RuleGroupRuleDataArgs(
            ref_id="C",
            datasource_uid="-100",
            relative_time_range=alerting.RuleGroupRuleDataRelativeTimeRangeArgs(
                from_=600, to=0
            ),
            model=json.dumps(
                {
                    "type": "threshold",
                    "datasource": {"type": "__expr__", "uid": "-100"},
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "query": {"params": []},
                            "type": "query",
                        }
                    ],
                    "refId": "C",
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
    apisix_edge.create(alerts_folder.uid, rd, resource_opts)
    dagster_pgbouncer.create(alerts_folder.uid, rd, resource_opts)
    # No folder_uid: these rules live in the Synthetic Monitoring plugin's own
    # folder rather than the one created above. See the module docstring.
    synthetic_monitoring.create(rd, resource_opts)
