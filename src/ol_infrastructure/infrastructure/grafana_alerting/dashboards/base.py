"""Grafana dashboards.

Every Grafana Cloud stack provisions its own Mimir and Loki datasource under
the same generic UIDs (see metric_rules/base.py and log_rules/base.py), so a
dashboard defined here renders each stack's own data once deployed there --
no per-environment branching needed.

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
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    MIMIR_DATASOURCE_REF,
)


def _timeseries_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    legend_format: str = "{{identity_provider}}",
) -> dict[str, Any]:
    """Build a time-series panel model querying a shared datasource."""
    return {
        "title": title,
        "type": "timeseries",
        "datasource": datasource_ref,
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
                "datasource": datasource_ref,
                "expr": expr,
                "legendFormat": legend_format,
                "refId": "A",
            }
        ],
    }


def _bar_gauge_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    legend_format: str = "{{identity_provider}}",
) -> dict[str, Any]:
    """Build a bar-gauge panel model querying a shared datasource."""
    return {
        "title": title,
        "type": "bargauge",
        "datasource": datasource_ref,
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
                "datasource": datasource_ref,
                "expr": expr,
                "legendFormat": legend_format,
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
