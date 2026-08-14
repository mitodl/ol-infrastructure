"""Trace- and broad-event-backed activity panels not covered by the overview.

Trimmed port of the Grafana-Cloud-UI-authored "Keycloak Activity Dashboard".
A few of that dashboard's panels ("Overall error ratio", "Current error
rates", "Event types breakdown") are strict subsets or alternate
visualizations of what keycloak_overview.py already shows from Keycloak's
own Micrometer events, so they're dropped here. What's kept is content
Overview has no equivalent for:

- Combined Tempo-request-rate + Loki-error-rate panels for the two
  browser-facing login/registration form endpoints. Overview only sees
  Keycloak's own `login`/`register` *events*; it has no visibility into
  raw HTTP hits on `/login-actions/authenticate` and
  `/login-actions/registration` themselves (e.g. a client that never
  completes the form flow far enough to emit a Keycloak event still shows
  up here).
- Tempo trace-derived request rates for endpoints Keycloak doesn't emit a
  `keycloak_user_events_total` event for at all (token exchange,
  authorization, account-management requests).
- Two broad, un-itemized event/error breakdowns from Loki (Overview only
  itemizes the event types and error codes we specifically care about).

The original dashboard hardcoded `service.name="keycloak-production"` and
`cluster="operations-production"`, so it only ever worked in the Production
stack. This version uses a `service.name` regex and a `$cluster` template
variable so it's portable across QA and Production.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
    TEMPO_DATASOURCE_REF,
)

_LOKI_SELECTOR = 'namespace="keycloak", container="keycloak", cluster=~"$cluster"'


def _trace_rate_expr(*, route_pattern: str) -> str:
    """TraceQL rate() for a Keycloak HTTP route, across QA and Production.

    Matches on the `span.http.route` semantic-convention attribute (the
    path template alone, e.g. "/realms/{realm}/protocol/{protocol}/auth")
    rather than the span `name` (which also carries the HTTP method prefix
    and requires a full, not partial, match under TraceQL's `=~`).
    """
    return (
        f'{{resource.service.name=~"keycloak.*" '
        f'&& span.http.route =~ "{route_pattern}"}} | rate()'
    )


def _loki_error_rate_expr(*, event_type: str) -> str:
    """LogQL rate() of a single raw Keycloak log event type, by logfmt."""
    return (
        "sum(rate("
        f'{{{_LOKI_SELECTOR}}} | logfmt | type =~ "{event_type}" [$__rate_interval]'
        "))"
    )


def _attempts_vs_errors_panel(
    timeseries_panel: Callable[..., dict[str, Any]],
    *,
    title: str,
    route_pattern: str,
    error_event_type: str,
    grid_pos: dict[str, Any],
) -> dict[str, Any]:
    """Build a combined Tempo request-rate + Loki error-rate panel."""
    return timeseries_panel(
        title=title,
        queries=[
            {
                "expr": _trace_rate_expr(route_pattern=route_pattern),
                "legend_format": "attempts/s",
                "datasource_ref": TEMPO_DATASOURCE_REF,
                "query_key": "query",
            },
            {
                "expr": _loki_error_rate_expr(event_type=error_event_type),
                "legend_format": "errors/s",
                "datasource_ref": LOKI_DATASOURCE_REF,
            },
        ],
        grid_pos=grid_pos,
    )


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "keycloak-activity",
        "title": "Keycloak - Activity",
        "description": (
            "Trace-derived request rates and broad event/error breakdowns "
            "for Keycloak, not already covered by Keycloak - Overview."
        ),
        "tags": ["keycloak"],
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
            row_panel(title="Login & Registration (Tempo + Loki)", y=0),
            _attempts_vs_errors_panel(
                timeseries_panel,
                title="Login Attempts vs Errors",
                route_pattern=".*/login-actions/authenticate",
                error_event_type="LOGIN_ERROR",
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 1},
            ),
            _attempts_vs_errors_panel(
                timeseries_panel,
                title="Registration Attempts vs Errors",
                route_pattern=".*/login-actions/registration",
                error_event_type="REGISTER_ERROR",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 1},
            ),
            row_panel(title="Request Rates (Tempo)", y=9),
            timeseries_panel(
                title="Token Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/protocol/.*/token"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 10},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="token requests/s",
                query_key="query",
            ),
            timeseries_panel(
                title="Authorization Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/protocol/.*/auth"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 10},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="authorization requests/s",
                query_key="query",
            ),
            timeseries_panel(
                title="Account Management Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/account/.*"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 18},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="account requests/s",
                query_key="query",
            ),
            row_panel(title="Broad Event/Error Breakdown (Loki)", y=26),
            timeseries_panel(
                title="All Event Types Rate",
                expr=(
                    "sum by (type) (rate("
                    f'{{{_LOKI_SELECTOR}}} | logfmt | type != "" [$__rate_interval]'
                    "))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 27},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{type}}",
            ),
            bar_gauge_panel(
                title="Top Error Types (selected range)",
                expr=(
                    "topk(5, sum by (error) (count_over_time("
                    f'{{{_LOKI_SELECTOR}}} | logfmt | error != "" [$__range]'
                    ")))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 27},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{error}}",
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the trimmed Keycloak activity dashboard."""
    create_dashboard(
        "keycloak-activity-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, bar_gauge_panel, row_panel),
        resource_opts,
    )
