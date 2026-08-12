"""Grafana dashboards.

Every Grafana Cloud stack provisions its own Mimir datasource under the same
generic UID (see metric_rules/base.py), so a dashboard defined here renders
each stack's own data once deployed there -- no per-environment branching
needed.

Sub-modules
-----------
  keycloak_olapps_idp_logins — Per-identity-provider login counts for the
    olapps realm.
"""

import json
from typing import Any

from pulumi import ResourceOptions
from pulumiverse_grafana.oss.dashboard import Dashboard
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.dashboards import (
    keycloak_olapps_idp_logins,
)

# Every Grafana Cloud stack provisions its own Mimir datasource with this same
# generic UID. The per-stack slug (e.g. grafanacloud-mitolci-prom) is only the
# datasource *name*; referencing it as a UID fails with "data source not found".
_MIMIR_DATASOURCE_UID = "grafanacloud-prom"
_DATASOURCE_REF = {"type": "prometheus", "uid": _MIMIR_DATASOURCE_UID}


def _timeseries_panel(
    *, title: str, expr: str, grid_pos: dict[str, Any]
) -> dict[str, Any]:
    """Build a time-series panel model querying the shared Mimir datasource."""
    return {
        "title": title,
        "type": "timeseries",
        "datasource": _DATASOURCE_REF,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "pointSize": 5,
                },
                "unit": "short",
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "calcs": ["sum"],
            },
            "tooltip": {"mode": "multi"},
        },
        "targets": [
            {
                "datasource": _DATASOURCE_REF,
                "expr": expr,
                "legendFormat": "{{identity_provider}}",
                "refId": "A",
            }
        ],
    }


def _bar_gauge_panel(
    *, title: str, expr: str, grid_pos: dict[str, Any]
) -> dict[str, Any]:
    """Build a bar-gauge panel model querying the shared Mimir datasource."""
    return {
        "title": title,
        "type": "bargauge",
        "datasource": _DATASOURCE_REF,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "unit": "short",
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "showUnfilled": True,
        },
        "targets": [
            {
                "datasource": _DATASOURCE_REF,
                "expr": expr,
                "legendFormat": "{{identity_provider}}",
                "refId": "A",
                "instant": True,
            }
        ],
    }


def _create_dashboard(
    resource_name: str,
    folder_uid,
    dashboard_json: dict[str, Any],
    resource_opts: ResourceOptions,
) -> None:
    Dashboard(
        resource_name,
        config_json=json.dumps(dashboard_json),
        folder=folder_uid,
        overwrite=True,
        opts=resource_opts,
    )


def create(resource_opts: ResourceOptions) -> None:
    """Create the dashboards folder and all Grafana dashboards."""
    dashboards_folder = Folder(
        "keycloak-dashboards-folder",
        title="Keycloak",
        uid="keycloak-dashboards",
        opts=resource_opts,
    )

    keycloak_olapps_idp_logins.create(
        dashboards_folder.uid,
        _timeseries_panel,
        _bar_gauge_panel,
        _create_dashboard,
        resource_opts,
    )
