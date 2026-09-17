"""Shared Grafana datasource references for the dashboards package.

Every Grafana Cloud stack provisions its own Mimir/Loki datasource under
these same generic UIDs (see metric_rules/base.py and log_rules/base.py).
The per-stack slug (e.g. grafanacloud-mitolci-prom) is only the datasource
*name*; referencing it as a UID fails with "data source not found".

Kept in its own module, rather than in base.py, so individual dashboard
sub-modules can import a specific datasource ref directly without a circular
import back through base.py (which imports every sub-module).
"""

MIMIR_DATASOURCE_REF = {"type": "prometheus", "uid": "grafanacloud-prom"}
LOKI_DATASOURCE_REF = {"type": "loki", "uid": "grafanacloud-logs"}

# Traces live in a per-stack Tempo instance registered the same way. Checked
# 2026-09-17 against all three stacks: the datasource *names* differ
# (grafanacloud-mitolci-traces, grafanacloud-mitolqa-traces,
# grafanacloud-mitolproduction-traces) but the UID is `grafanacloud-traces` on
# each, so a dashboard referencing it renders on whichever stack it lands in.
TEMPO_DATASOURCE_REF = {"type": "tempo", "uid": "grafanacloud-traces"}
