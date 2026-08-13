"""Trace- and broad-event-backed activity panels not covered by the overview.

Trimmed port of the Grafana-Cloud-UI-authored "Keycloak Activity Dashboard".
Most of that dashboard's panels (login/registration rate, error ratio,
current error rates) are strict subsets of what keycloak_overview.py already
shows from Keycloak's own Micrometer events, so they're dropped here. What's
kept is content Overview has no equivalent for: Tempo trace-derived request
rates for endpoints Keycloak doesn't emit a `keycloak_user_events_total`
event for (token exchange, authorization, account-management requests), and
two broad, un-itemized event/error breakdowns from Loki (Overview only
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
            row_panel(title="Request Rates (Tempo)", y=0),
            timeseries_panel(
                title="Token Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/protocol/.*/token"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 1},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="token requests/s",
                query_key="query",
            ),
            timeseries_panel(
                title="Authorization Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/protocol/.*/auth"),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 1},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="authorization requests/s",
                query_key="query",
            ),
            timeseries_panel(
                title="Account Management Request Rate",
                expr=_trace_rate_expr(route_pattern=".*/account/.*"),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 9},
                datasource_ref=TEMPO_DATASOURCE_REF,
                legend_format="account requests/s",
                query_key="query",
            ),
            row_panel(title="Broad Event/Error Breakdown (Loki)", y=17),
            timeseries_panel(
                title="All Event Types Rate",
                expr=(
                    "sum by (type) (rate("
                    f'{{{_LOKI_SELECTOR}}} | logfmt | type != "" [$__rate_interval]'
                    "))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 18},
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
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 18},
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
