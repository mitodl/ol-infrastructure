"""Altinity's ClickHouse operator and Keeper dashboards, vendored unchanged.

Source: grafana-dashboard/ in Altinity/clickhouse-operator at release-0.26.0,
matching CLICKHOUSE_OPERATOR_VERSION. The operator dashboard is grafana.com
12163. Kept as upstream JSON rather than rebuilt with base.py's panel helpers:
they are large, maintained upstream against the exporter's own metric names,
and re-vendoring on an operator upgrade is a file copy.

Both select their datasource through a ``DS_PROMETHEUS`` template variable, so
they pick up each stack's default Mimir datasource without editing. The labels
they filter on (``app``, ``chi``, ``hostname`` for the operator; ``app``,
``pod_name``, ``container_name`` for Keeper) are supplied by relabelings on the
ServiceMonitors in substructure/aws/eks/clickhouse_operator.py and
applications/clickhouse.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pulumi import Input, ResourceOptions

_VENDOR_DIR = Path(__file__).parent / "vendor"
_DASHBOARDS = {
    "clickhouse-operator-dashboard": "altinity_clickhouse_operator.json",
    "clickhouse-keeper-dashboard": "altinity_clickhouse_keeper.json",
}


def _load(filename: str) -> dict[str, Any]:
    dashboard = json.loads((_VENDOR_DIR / filename).read_text())
    # Import-time metadata for grafana.com's UI importer; the provisioning API
    # rejects a numeric id that does not exist and ignores the rest.
    for key in ("__inputs", "__requires", "__elements", "id"):
        dashboard.pop(key, None)
    return dashboard


def create(
    folder_uid: Input[str],
    create_dashboard: Callable[..., None],
    resource_opts: ResourceOptions,
) -> None:
    """Create the vendored ClickHouse dashboards."""
    for resource_name, filename in _DASHBOARDS.items():
        create_dashboard(resource_name, folder_uid, _load(filename), resource_opts)
