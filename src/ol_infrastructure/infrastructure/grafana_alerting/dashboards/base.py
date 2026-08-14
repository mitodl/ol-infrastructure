"""Grafana dashboards.

Every Grafana Cloud stack provisions its own Mimir and Loki datasource under
the same generic UIDs (see metric_rules/base.py and log_rules/base.py), so a
dashboard defined here renders each stack's own data once deployed there --
no per-environment branching needed.

Sub-modules
-----------
  keycloak_overview — General service-health overview (logins, JVM, HTTP,
    DB pool) across all realms.
  keycloak_olapps_realm — Holistic authentication-activity view (logins,
    registrations, token flows, per-identity-provider breakdown) for just
    the olapps realm.
"""

import json
from typing import Any

from pulumi import Input, ResourceOptions
from pulumiverse_grafana.oss.dashboard import Dashboard
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.dashboards import (
    keycloak_olapps_realm,
    keycloak_overview,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
    MIMIR_DATASOURCE_REF,
)


def _timeseries_panel(
    *,
    title: str,
    expr: str = "",
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    legend_format: str = "{{identity_provider}}",
    queries: list[dict[str, Any]] | None = None,
    unit: str = "short",
    decimals: int | None = None,
    legend_calc: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    """Build a time-series panel model querying a shared datasource.

    Pass `queries` (a list of `{"expr": ..., "legend_format": ...}` dicts)
    instead of `expr`/`legend_format` when a panel needs more than one
    query series (e.g. p50/p95/p99 latency, or GC time+count by cause) --
    each becomes its own target, lettered A, B, C...

    `decimals` forces the displayed precision (e.g. `0` to round a
    per-minute rate to a whole number) instead of Grafana's auto-scaled
    default, which otherwise shows several decimal places for a small
    rate value.

    `description` shows as a hover tooltip (the small "i" icon in the panel
    header) -- use it to spell out what a value actually means when that
    isn't obvious from the title alone, e.g. that a count is per graph
    interval rather than a total.

    The legend's summary column defaults to a straight `sum` across the
    graph window, which is the right read for a count/rate series but
    meaningless for anything that's a gauge/instantaneous reading rather
    than an accumulating count -- e.g. summing a ratio produces "3282%",
    and summing a heap-usage sample across a day's worth of data points
    produces a "total" bytes figure with no physical meaning (real max
    heap was 2.5 GiB, not the 1.73 TiB the naive sum reported). Ratio
    units (`percentunit`/`percent`) default to `mean` automatically.
    Pass `legend_calc` explicitly for any other gauge-like series (e.g.
    `"max"` for a memory panel, to show peak usage over the window).
    """
    if queries is None:
        queries = [{"expr": expr, "legend_format": legend_format}]
    targets = [
        {
            "datasource": datasource_ref,
            "expr": query["expr"],
            "legendFormat": query.get("legend_format", "{{legend}}"),
            "refId": chr(65 + i),
        }
        for i, query in enumerate(queries)
    ]
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": 10,
            "pointSize": 5,
        },
        "unit": unit,
        "min": 0,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if legend_calc is None:
        legend_calc = "mean" if unit in ("percentunit", "percent") else "sum"
    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": defaults,
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "calcs": [legend_calc],
            },
            "tooltip": {"mode": "multi"},
        },
        "targets": targets,
    }


def _bar_gauge_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    legend_format: str = "{{identity_provider}}",
    unit: str = "short",
    decimals: int | None = None,
) -> dict[str, Any]:
    """Build a bar-gauge panel model querying a shared datasource."""
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "unit": unit,
        "min": 0,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "title": title,
        "type": "bargauge",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": defaults,
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


def _stat_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    unit: str = "short",
    decimals: int | None = None,
    legend_format: str = "",
) -> dict[str, Any]:
    """Build a single-value stat panel querying a shared datasource."""
    defaults: dict[str, Any] = {
        "color": {"mode": "thresholds"},
        "unit": unit,
        "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}],
        },
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "title": title,
        "type": "stat",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": defaults,
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
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


def _gauge_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    unit: str = "percentunit",
    min_value: float = 0,
    max_value: float = 1,
) -> dict[str, Any]:
    """Build a radial gauge panel querying a shared datasource."""
    return {
        "title": title,
        "type": "gauge",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "unit": unit,
                "min": min_value,
                "max": max_value,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "yellow", "value": 0.95},
                        {"color": "green", "value": 0.99},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
        "targets": [
            {
                "datasource": datasource_ref,
                "expr": expr,
                "refId": "A",
                "instant": True,
            }
        ],
    }


def _logs_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = LOKI_DATASOURCE_REF,
) -> dict[str, Any]:
    """Build a raw log-line panel querying the Loki datasource."""
    return {
        "title": title,
        "type": "logs",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "options": {
            "showTime": True,
            "showLabels": False,
            "showCommonLabels": False,
            "wrapLogMessage": True,
            "prettifyLogMessage": False,
            "enableLogDetails": True,
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
        "targets": [
            {
                "datasource": datasource_ref,
                "expr": expr,
                "refId": "A",
            }
        ],
    }


def _row_panel(*, title: str, y: int) -> dict[str, Any]:
    """Build a row divider panel to visually group the panels beneath it."""
    return {
        "title": title,
        "type": "row",
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def _create_dashboard(
    resource_name: str,
    folder_uid: Input[str],
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

    keycloak_overview.create(
        dashboards_folder.uid,
        _timeseries_panel,
        _stat_panel,
        _gauge_panel,
        _logs_panel,
        _row_panel,
        _create_dashboard,
        resource_opts,
    )

    keycloak_olapps_realm.create(
        dashboards_folder.uid,
        _timeseries_panel,
        _stat_panel,
        _bar_gauge_panel,
        _logs_panel,
        _row_panel,
        _create_dashboard,
        resource_opts,
    )
