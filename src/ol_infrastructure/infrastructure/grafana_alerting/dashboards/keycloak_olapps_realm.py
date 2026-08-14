"""Holistic view of authentication activity in the olapps Keycloak realm.

For devs and management to gauge, at a glance, how the olapps realm is
doing: login/registration/token flow volumes and error rates from
Keycloak's own Micrometer counters (`keycloak_user_events_total`, scoped to
`realm="olapps"` -- an exhaustive counter, not a sample), plus a
per-identity-provider breakdown of IdP-brokered logins from Loki (folded in
from the now-retired "Keycloak - olapps IdP Logins" dashboard). Deliberately
excludes hardware/JVM/pod metrics -- those live on keycloak_overview.py,
which covers every realm rather than just olapps.

An earlier version of the sibling "Activity" dashboard paired Tempo trace
counts against Loki event counts as "attempts vs errors". That comparison
was misleading: Tempo only sees a sampled subset of requests while Loki
sees every one (Keycloak logs every login event unconditionally), so the
two numbers were never on the same footing and didn't answer a coherent
question. Everything here is deliberately drawn from exhaustive sources --
Prometheus counters and raw log lines -- so the numbers mean what they say.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)

_REALM_SELECTOR = 'namespace="keycloak", realm="olapps"'


def _idp_login_count_expr(*, level: str, event_types: str, window: str) -> str:
    """LogQL counting olapps IdP-brokered login events by identity_provider.

    `event_types` is a regex alternation (e.g. "LOGIN|IDENTITY_PROVIDER_LOGIN")
    matched against Keycloak's own `type="..."` field. Restricting to lines
    that also carry `identity_provider="` scopes this to logins actually
    brokered through an external IdP, excluding direct username/password
    logins to olapps (which have no identity_provider and aren't this
    panel's concern -- those are covered by the Micrometer-derived panels
    above instead).
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
    stat_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    idp_success_kwargs = {
        "level": "INFO",
        "event_types": "LOGIN|IDENTITY_PROVIDER_LOGIN",
    }
    idp_failure_kwargs = {
        "level": "WARN",
        "event_types": "LOGIN_ERROR|IDENTITY_PROVIDER_LOGIN_ERROR",
    }
    return {
        "uid": "keycloak-olapps-realm",
        "title": "Keycloak - olapps Realm",
        "description": (
            "Holistic view of authentication activity in the olapps realm -- "
            "logins, registrations, token flows, and per-identity-provider "
            "breakdowns -- for devs and management to gauge how the realm is "
            "doing. See Keycloak - Overview for hardware/JVM metrics."
        ),
        "tags": ["keycloak", "olapps"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "templating": {
            "list": [
                {
                    "name": "cluster",
                    "type": "query",
                    "datasource": LOKI_DATASOURCE_REF,
                    "query": 'label_values({namespace="keycloak"},cluster)',
                    "multi": True,
                    "includeAll": True,
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                },
            ]
        },
        "panels": [
            row_panel(title="olapps Realm At a Glance", y=0),
            stat_panel(
                title="Logins (selected range)",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="login", error="", {_REALM_SELECTOR}}}'
                    "[$__range]))"
                ),
                grid_pos={"h": 4, "w": 6, "x": 0, "y": 1},
            ),
            stat_panel(
                title="Login Error %",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="login", error!="", {_REALM_SELECTOR}}}'
                    "[$__range]))\n/\n"
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="login", {_REALM_SELECTOR}}}'
                    "[$__range]))"
                ),
                grid_pos={"h": 4, "w": 6, "x": 6, "y": 1},
                unit="percentunit",
            ),
            stat_panel(
                title="Registrations (selected range)",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="register", error="", {_REALM_SELECTOR}}}'
                    "[$__range]))"
                ),
                grid_pos={"h": 4, "w": 6, "x": 12, "y": 1},
            ),
            stat_panel(
                title="Account Lockouts (selected range)",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{error=~"user_disabled|user_temporarily_disabled", {_REALM_SELECTOR}}}'
                    "[$__range]))"
                ),
                grid_pos={"h": 4, "w": 6, "x": 18, "y": 1},
            ),
            row_panel(title="Login & Auth Flow Trends", y=5),
            timeseries_panel(
                title="Logins vs Failures per Minute",
                queries=[
                    {
                        "expr": (
                            "sum(rate(keycloak_user_events_total"
                            f'{{event="login", error="", {_REALM_SELECTOR}}}'
                            "[$__rate_interval])) * 60"
                        ),
                        "legend_format": "logins/min",
                    },
                    {
                        "expr": (
                            "sum(rate(keycloak_user_events_total"
                            f'{{event="login", error!="", {_REALM_SELECTOR}}}'
                            "[$__rate_interval])) * 60"
                        ),
                        "legend_format": "failures/min",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 6},
            ),
            timeseries_panel(
                title="Login Errors by Type",
                expr=(
                    "sum by (error) (rate(keycloak_user_events_total"
                    f'{{event="login", error!="", {_REALM_SELECTOR}}}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 6},
                legend_format="{{error}}",
            ),
            timeseries_panel(
                title="Login & Logout Events",
                expr=(
                    "sum by (event) (rate(keycloak_user_events_total"
                    f'{{event=~"login|logout", {_REALM_SELECTOR}}}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 14},
                legend_format="{{event}}",
            ),
            timeseries_panel(
                title="Token Operations & Registrations",
                expr=(
                    "sum by (event) (rate(keycloak_user_events_total"
                    '{event=~"refresh_token|code_to_token|register|token_exchange", '
                    f"{_REALM_SELECTOR}}}"
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 14},
                legend_format="{{event}}",
            ),
            row_panel(title="Logins by Identity Provider (Loki)", y=22),
            timeseries_panel(
                title="Successful Logins by Identity Provider",
                expr=_idp_login_count_expr(**idp_success_kwargs, window="$__interval"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 23},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            timeseries_panel(
                title="Failed Logins by Identity Provider",
                expr=_idp_login_count_expr(**idp_failure_kwargs, window="$__interval"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 23},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            bar_gauge_panel(
                title="Total Successful Logins by IdP (selected range)",
                expr=_idp_login_count_expr(**idp_success_kwargs, window="$__range"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 31},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            bar_gauge_panel(
                title="Total Failed Logins by IdP (selected range)",
                expr=_idp_login_count_expr(**idp_failure_kwargs, window="$__range"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 31},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            row_panel(title="Recent Auth Failures (Logs)", y=39),
            logs_panel(
                title="olapps Auth Event Failures",
                expr=(
                    '{namespace="keycloak", container="keycloak", cluster=~"$cluster"} '
                    '|= `realmId="olapps"` '
                    '|~ `type="(LOGIN_ERROR|REGISTER_ERROR|'
                    'IDENTITY_PROVIDER_LOGIN_ERROR)"`'
                ),
                grid_pos={"h": 8, "w": 24, "x": 0, "y": 40},
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the olapps realm holistic activity dashboard."""
    create_dashboard(
        "keycloak-olapps-realm-dashboard",
        folder_uid,
        _dashboard_json(
            timeseries_panel, stat_panel, bar_gauge_panel, logs_panel, row_panel
        ),
        resource_opts,
    )
