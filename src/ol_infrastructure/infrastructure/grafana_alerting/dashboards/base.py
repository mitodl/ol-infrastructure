"""Grafana dashboards.

Every Grafana Cloud stack provisions its own Mimir and Loki datasource under
the same generic UIDs (see metric_rules/base.py and log_rules/base.py), so a
dashboard defined here renders each stack's own data once deployed there --
no per-environment branching needed.

Sub-modules
-----------
  keycloak_olapps_idp_logins — Per-identity-provider login counts for the
    olapps realm.
  keycloak_overview — General service-health overview (logins, JVM, HTTP,
    DB pool) across all realms.
  keycloak_activity — Trace-backed request rates not covered by
    keycloak_overview's Micrometer-derived panels.
"""

import json
import re
from typing import Any

from pulumi import Input, ResourceOptions
from pulumiverse_grafana.oss.dashboard import Dashboard
from pulumiverse_grafana.oss.folder import Folder

from ol_infrastructure.infrastructure.grafana_alerting.dashboards import (
    keycloak_activity,
    keycloak_olapps_idp_logins,
    keycloak_overview,
)
from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
    MIMIR_DATASOURCE_REF,
)

_MIXED_DATASOURCE_REF = {"type": "datasource", "uid": "-- Mixed --"}

# Tempo's TraceQL metrics queries always end with the metric function that
# produces the series (e.g. "... | count_over_time()"), and that function
# name -- not `legendFormat` -- is what Tempo names an ungrouped series
# after. Extracted so a field override can force the intended display name.
_TEMPO_METRIC_FUNC_RE = re.compile(r"\|\s*([a-zA-Z_]+)\(")


def _tempo_metric_name(expr: str) -> str | None:
    """Return the last TraceQL metrics function name used in `expr`, if any."""
    matches = _TEMPO_METRIC_FUNC_RE.findall(expr)
    return matches[-1] if matches else None


def _timeseries_panel(
    *,
    title: str,
    expr: str = "",
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    legend_format: str = "{{identity_provider}}",
    queries: list[dict[str, Any]] | None = None,
    unit: str = "short",
    query_key: str = "expr",
    description: str = "",
) -> dict[str, Any]:
    """Build a time-series panel model querying a shared datasource.

    Pass `queries` (a list of `{"expr": ..., "legend_format": ...}` dicts)
    instead of `expr`/`legend_format` when a panel needs more than one
    query series (e.g. p50/p95/p99 latency, or GC time+count by cause) --
    each becomes its own target, lettered A, B, C...

    `query_key` names the JSON key the datasource expects the query string
    under -- Prometheus/Loki use `expr`, but Tempo's TraceQL targets use
    `query` instead; pass `query_key="query"` for a Tempo-backed panel.
    Either can be overridden per-query (via a `"query_key"`/`"datasource_ref"`
    key in that query's dict) for a panel comparing series from two
    datasources -- e.g. a Tempo request rate next to a Loki error rate for
    the same endpoint. The panel's own `datasource` is set to Grafana's
    mixed-datasource sentinel whenever the targets don't all share one.

    `description` shows as a hover tooltip (the small "i" icon in the panel
    header) -- use it to spell out what a value actually means when that
    isn't obvious from the title alone, e.g. that a count is per graph
    interval rather than a total.
    """
    if queries is None:
        queries = [{"expr": expr, "legend_format": legend_format}]
    resolved_datasources = [
        query.get("datasource_ref", datasource_ref) for query in queries
    ]
    targets = [
        {
            "datasource": resolved_ds,
            query.get("query_key", query_key): query["expr"],
            "legendFormat": query.get("legend_format", "{{legend}}"),
            "refId": chr(65 + i),
        }
        for i, (query, resolved_ds) in enumerate(zip(queries, resolved_datasources))
    ]
    unique_uids = {ds["uid"] for ds in resolved_datasources}
    panel_datasource = (
        resolved_datasources[0] if len(unique_uids) == 1 else _MIXED_DATASOURCE_REF
    )
    # Tempo's TraceQL metrics queries ignore `legendFormat` for a query with
    # no `by()` grouping -- the series is named after the metric function
    # instead (e.g. "count_over_time"), and a `byFrameRefID` override does
    # not override that name either. Matching the field by that generated
    # name and overriding its display name does work, so a static
    # legend_format ends up honored the same way it already is for
    # Prometheus/Loki targets.
    overrides = []
    for query, resolved_ds in zip(queries, resolved_datasources):
        if resolved_ds["type"] != "tempo":
            continue
        query_legend_format = query.get("legend_format", "{{legend}}")
        if "{{" in query_legend_format:
            continue
        metric_name = _tempo_metric_name(query["expr"])
        if metric_name is None:
            continue
        overrides.append(
            {
                "matcher": {"id": "byName", "options": metric_name},
                "properties": [{"id": "displayName", "value": query_legend_format}],
            }
        )
    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": panel_datasource,
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
                "unit": unit,
                "min": 0,
            },
            "overrides": overrides,
        },
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "calcs": ["sum"],
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
                "unit": unit,
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


def _stat_panel(
    *,
    title: str,
    expr: str,
    grid_pos: dict[str, Any],
    datasource_ref: dict[str, str] = MIMIR_DATASOURCE_REF,
    unit: str = "short",
    legend_format: str = "",
) -> dict[str, Any]:
    """Build a single-value stat panel querying a shared datasource."""
    return {
        "title": title,
        "type": "stat",
        "datasource": datasource_ref,
        "gridPos": grid_pos,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "unit": unit,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
            },
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

    keycloak_olapps_idp_logins.create(
        dashboards_folder.uid,
        _timeseries_panel,
        _bar_gauge_panel,
        _create_dashboard,
        resource_opts,
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

    keycloak_activity.create(
        dashboards_folder.uid,
        _timeseries_panel,
        _bar_gauge_panel,
        _row_panel,
        _create_dashboard,
        resource_opts,
    )
