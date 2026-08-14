"""Trace- and broad-event-backed activity panels not covered by the overview.

Trimmed port of the Grafana-Cloud-UI-authored "Keycloak Activity Dashboard".
A few of that dashboard's panels ("Overall error ratio", "Current error
rates", "Event types breakdown") are strict subsets or alternate
visualizations of what keycloak_overview.py already shows from Keycloak's
own Micrometer events, so they're dropped here. What's kept is content
Overview has no equivalent for:

- Combined Tempo-request-count + Loki-error-count panels for the two
  browser-facing login/registration form endpoints. Overview only sees
  Keycloak's own `login`/`register` *events*; it has no visibility into
  raw HTTP hits on `/login-actions/authenticate` and
  `/login-actions/registration` themselves (e.g. a client that never
  completes the form flow far enough to emit a Keycloak event still shows
  up here).
- Tempo trace-derived request counts for endpoints Keycloak doesn't emit a
  `keycloak_user_events_total` event for at all (token exchange,
  authorization, account-management requests).
- Two broad, un-itemized event/error breakdowns from Loki (Overview only
  itemizes the event types and error codes we specifically care about).

Every panel here reports a plain count per graph interval rather than a
per-second `rate()` -- at Keycloak's human-scale traffic (logins per minute,
not per second), a rate reads as an unreadable tiny decimal (e.g.
`0.0000347`) where a count reads as "3". The tradeoff is that the bucket
width isn't a fixed unit -- it auto-scales with the selected time range and
panel width, same as any Grafana time series -- so each panel's description
(hover the "i" icon) spells out what's being counted.

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

# All panels below report counts per graph interval (the bucket width Grafana
# picks for the current time range and panel width), not a per-second rate --
# a raw count like "3" reads far more clearly than "0.0002/s" for traffic this
# low-volume, at the cost of the bucket width being implicit rather than a
# fixed unit. Panel descriptions spell this out for anyone hovering the "i" icon.
_COUNT_DESCRIPTION_SUFFIX = (
    " Counted per graph interval (bucket width auto-scales with the "
    "selected time range), not a per-second rate."
)


def _trace_count_expr(*, route_pattern: str) -> str:
    """TraceQL count_over_time() for a Keycloak HTTP route, QA and Production.

    Matches on the `span.http.route` semantic-convention attribute (the
    path template alone, e.g. "/realms/{realm}/protocol/{protocol}/auth")
    rather than the span `name` (which also carries the HTTP method prefix
    and requires a full, not partial, match under TraceQL's `=~`).
    """
    return (
        f'{{resource.service.name=~"keycloak.*" '
        f'&& span.http.route =~ "{route_pattern}"}} | count_over_time()'
    )


def _loki_error_count_expr(*, event_type: str) -> str:
    """LogQL count_over_time() of a single raw Keycloak log event type."""
    return (
        "sum(count_over_time("
        f'{{{_LOKI_SELECTOR}}} | logfmt | type =~ "{event_type}" [$__interval]'
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
    """Build a combined Tempo request-count + Loki error-count panel."""
    return timeseries_panel(
        title=title,
        description=(
            "'attempts' is a count of matching HTTP requests seen in traces "
            "(Tempo); 'errors' is a count of matching log lines (Loki)."
            + _COUNT_DESCRIPTION_SUFFIX
        ),
        queries=[
            {
                "expr": _trace_count_expr(route_pattern=route_pattern),
                "legend_format": "attempts",
                "datasource_ref": TEMPO_DATASOURCE_REF,
                "query_key": "query",
            },
            {
                "expr": _loki_error_count_expr(event_type=error_event_type),
                "legend_format": "errors",
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
            "Trace-derived request counts and broad event/error breakdowns "
            "for Keycloak, not already covered by Keycloak - Overview. Values "
            "are counts per graph interval, not per-second rates -- hover a "
            'panel\'s "i" icon for what it counts.'
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
            row_panel(title="Request Counts (Tempo)", y=9),
            timeseries_panel(
                title="Token Requests",
                description="Count of token-endpoint requests."
                + _COUNT_DESCRIPTION_SUFFIX,
                expr=_trace_count_expr(route_pattern=".*/protocol/.*/token"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 10},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="token requests",
                query_key="query",
            ),
            timeseries_panel(
                title="Authorization Requests",
                description="Count of authorization-endpoint requests."
                + _COUNT_DESCRIPTION_SUFFIX,
                expr=_trace_count_expr(route_pattern=".*/protocol/.*/auth"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 10},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="authorization requests",
                query_key="query",
            ),
            timeseries_panel(
                title="Account Management Requests",
                description="Count of account-management requests."
                + _COUNT_DESCRIPTION_SUFFIX,
                expr=_trace_count_expr(route_pattern=".*/account/.*"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 18},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="account requests",
                query_key="query",
            ),
            row_panel(title="Broad Event/Error Breakdown (Loki)", y=26),
            timeseries_panel(
                title="All Event Types",
                description="Count of log lines per Keycloak event type."
                + _COUNT_DESCRIPTION_SUFFIX,
                expr=(
                    "sum by (type) (count_over_time("
                    f'{{{_LOKI_SELECTOR}}} | logfmt | type != "" [$__interval]'
                    "))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 27},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{type}}",
            ),
            bar_gauge_panel(
                title="Top Error Types (total, selected range)",
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
