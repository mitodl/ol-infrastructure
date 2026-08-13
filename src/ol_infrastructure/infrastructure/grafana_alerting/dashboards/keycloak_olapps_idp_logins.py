"""Per-identity-provider login dashboard for the olapps Keycloak realm.

Backed directly by Keycloak's own login events in Loki (see
substructure/aws/eks/grafana.py for the Alloy stage that redacts username and
identity_provider_identity -- the only PII these events carry -- before they
reach Loki; identity_provider itself is just an IdP alias, not PII).

This intentionally counts from raw logs via LogQL's count_over_time rather
than from an Alloy-derived Prometheus counter. A `metric.counter` component
only exists once it's been incremented, so Prometheus's increase() can't see
its first-ever appearance (no prior "0" sample to diff against), and the
counter itself gets dropped and recreated after any idle gap or Alloy
reload -- both silently undercounting. count_over_time reads straight from
Loki's stored log lines at query time instead: no persistent counter state,
so no cold-start gap and no eviction/reload resets to work around.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)


def _login_count_expr(*, level: str, event_types: str, window: str) -> str:
    """LogQL counting olapps IdP-brokered login events by identity_provider.

    `event_types` is a regex alternation (e.g. "LOGIN|IDENTITY_PROVIDER_LOGIN")
    matched against Keycloak's own `type="..."` field. Restricting to lines
    that also carry `identity_provider="` scopes this to logins actually
    brokered through an external IdP, excluding direct username/password
    logins to olapps (which have no identity_provider and aren't this
    dashboard's concern).
    """
    selector = (
        '{namespace="keycloak", container="keycloak"} '
        f'|= "{level}  [org.keycloak.events]" '
        '|= `realmId="olapps"` '
        f'|~ `type="({event_types})"` '
        '|= `identity_provider="`'
    )
    extract = ' | regexp `identity_provider="(?P<identity_provider>[^"]+)"`'
    return (
        f"sum by (identity_provider) (count_over_time({selector}{extract}[{window}]))"
    )


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    success_kwargs = {"level": "INFO", "event_types": "LOGIN|IDENTITY_PROVIDER_LOGIN"}
    failure_kwargs = {
        "level": "WARN",
        "event_types": "LOGIN_ERROR|IDENTITY_PROVIDER_LOGIN_ERROR",
    }
    return {
        "uid": "keycloak-olapps-idp-logins",
        "title": "Keycloak - olapps IdP Logins",
        "description": (
            "Per-identity-provider login counts for the olapps realm. "
            "Sourced directly from Keycloak's own success/failure login "
            "events in Loki -- username and identity_provider_identity are "
            "redacted by Alloy before ingestion; no other PII is shipped or "
            "queried for this."
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
                expr=_login_count_expr(**success_kwargs, window="$__interval"),
                grid_pos={"h": 9, "w": 12, "x": 0, "y": 0},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            timeseries_panel(
                title="Failed logins by identity provider",
                expr=_login_count_expr(**failure_kwargs, window="$__interval"),
                grid_pos={"h": 9, "w": 12, "x": 12, "y": 0},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            bar_gauge_panel(
                title="Total successful logins (selected range)",
                expr=_login_count_expr(**success_kwargs, window="$__range"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 9},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            bar_gauge_panel(
                title="Total failed logins (selected range)",
                expr=_login_count_expr(**failure_kwargs, window="$__range"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 9},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the olapps per-IdP login dashboard."""
    create_dashboard(
        "keycloak-olapps-idp-logins-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, bar_gauge_panel),
        resource_opts,
    )
