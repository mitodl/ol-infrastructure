"""Per-identity-provider login dashboard for the olapps Keycloak realm.

Backed by the keycloak_olapps_idp_login_total /
keycloak_olapps_idp_login_failure_total Prometheus counters that Alloy
extracts from Keycloak's own login events (see substructure/aws/eks/grafana.py)
-- no login record, or the PII it carries, is shipped to Loki for this; only
the aggregate counters are.
"""

from collections.abc import Callable
from typing import Any

from pulumi import ResourceOptions


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "keycloak-olapps-idp-logins",
        "title": "Keycloak - olapps IdP Logins",
        "description": (
            "Per-identity-provider login counts for the olapps realm. "
            "Sourced from Prometheus counters Alloy extracts from Keycloak's "
            "own success/failure login events -- no login record, or the "
            "PII it carries, is shipped to Loki for this."
        ),
        "tags": ["keycloak", "olapps"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-7d", "to": "now"},
        "refresh": "5m",
        "panels": [
            timeseries_panel(
                title="Successful logins by identity provider",
                expr=(
                    "sum by (identity_provider) ("
                    "increase(keycloak_olapps_idp_login_total[$__rate_interval]))"
                ),
                grid_pos={"h": 9, "w": 12, "x": 0, "y": 0},
            ),
            timeseries_panel(
                title="Failed logins by identity provider",
                expr=(
                    "sum by (identity_provider) ("
                    "increase(keycloak_olapps_idp_login_failure_total[$__rate_interval]))"
                ),
                grid_pos={"h": 9, "w": 12, "x": 12, "y": 0},
            ),
            bar_gauge_panel(
                title="Total successful logins (selected range)",
                expr=(
                    "sum by (identity_provider) ("
                    "increase(keycloak_olapps_idp_login_total[$__range]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 9},
            ),
            bar_gauge_panel(
                title="Total failed logins (selected range)",
                expr=(
                    "sum by (identity_provider) ("
                    "increase(keycloak_olapps_idp_login_failure_total[$__range]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 9},
            ),
        ],
    }


def create(
    folder_uid,
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[[str, object, dict[str, Any], ResourceOptions], None],
    resource_opts: ResourceOptions,
) -> None:
    """Create the olapps per-IdP login dashboard."""
    create_dashboard(
        "keycloak-olapps-idp-logins-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, bar_gauge_panel),
        resource_opts,
    )
