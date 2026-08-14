"""Ad-hoc incident-investigation dashboard for Keycloak, across all realms.

Every other dashboard in this package is an aggregate health view (Overview,
olapps Realm) -- neither is built for "a user reported a login problem, what
actually happened?" That today means grep-ing raw Loki lines by hand. This
dashboard replaces that with one filtered event table plus a handful of
aggregations that answer the questions that come up on every ticket: is this
isolated or a spike, which realm/client is affected, and is a specific
identity provider down.

All panels are Loki-backed. Keycloak's own event logger
(`org.keycloak.events`) is the only source with client/IP/identity-provider
granularity -- the `keycloak_user_events_total` Prometheus counter used by
the other dashboards only carries a `realm` label, not `client` or
`identity_provider`, so it can't answer "which client" or "which IdP"
questions at all.

Every field is pulled from the same raw log line via chained `| regexp`
stages rather than `| logfmt`: the line is comma-separated
(`type="LOGIN_ERROR", realmId="...", ...`), not space-separated logfmt, so
`| logfmt` does not parse it reliably. Each `| regexp` stage searches the
whole line independently, so field order doesn't matter and a line missing
a field (e.g. a plain LOGIN has no `error=` field at all) just leaves that
label unset rather than failing the match -- confirmed live against
production data before writing this.

You cannot search by username or email here -- Alloy redacts both before
they reach Loki (see keycloak_overview.py's docstring history / the
now-retired olapps IdP logins dashboard). The practical lookup keys are
`$realm`, `$client`, `$identity_provider`, and IP address, plus a free-text
`$search` box for anything else (a redirect_uri, a specific error string).
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)

_SELECTOR = 'namespace="keycloak", container="keycloak", cluster=~"$cluster"'
_KC_EVENT_FILTER = '|= "[org.keycloak.events]"'

# Every panel below extracts the same set of fields from the same raw
# Keycloak event line, then narrows with the dashboard's template
# variables. Centralized so the field list can't drift between panels.
_EXTRACT = (
    '| regexp `type="(?P<event_type>[A-Z_]+)"` '
    '| regexp `realmId="(?P<realm>[^"]*)"` '
    '| regexp `clientId="(?P<client>[^"]*)"` '
    '| regexp `ipAddress="(?P<ip>[^"]*)"` '
    '| regexp `error="(?P<error>[^"]*)"` '
    '| regexp `identity_provider="(?P<identity_provider>[^"]*)"`'
)
_NARROW = (
    '| realm=~"$realm" | client=~"$client" | identity_provider=~"$identity_provider"'
)


def _base_pipeline() -> str:
    """Build the shared selector + search + field-extraction + narrowing pipeline."""
    return f'{{{_SELECTOR}}} {_KC_EVENT_FILTER} |~ "$search" {_EXTRACT} {_NARROW}'


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "keycloak-incident-lookup",
        "title": "Keycloak - Incident Lookup",
        "description": (
            "Investigate a specific Keycloak auth report without grep-ing "
            "raw logs: filter by realm/client/identity-provider/free-text "
            "and get an event timeline plus the aggregations that answer "
            "'is this isolated or a spike' and 'which realm/client/IdP is "
            "affected'. Cannot search by username/email -- both are "
            "redacted before reaching Loki."
        ),
        "tags": ["keycloak"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-6h", "to": "now"},
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
                {
                    "name": "realm",
                    "type": "textbox",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
                {
                    "name": "client",
                    "type": "textbox",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
                {
                    "name": "identity_provider",
                    "type": "textbox",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
                {
                    "name": "search",
                    "type": "textbox",
                    "query": "",
                    "current": {"text": "", "value": ""},
                },
            ]
        },
        "panels": [
            row_panel(title="Incident Timeline", y=0),
            logs_panel(
                title="Event Timeline",
                expr=(
                    f"{_base_pipeline()} "
                    "| line_format `{{.event_type}} | realm={{.realm}} "
                    "client={{.client}} error={{.error}} "
                    "idp={{.identity_provider}} ip={{.ip}}`"
                ),
                grid_pos={"h": 10, "w": 24, "x": 0, "y": 1},
                datasource_ref=LOKI_DATASOURCE_REF,
            ),
            row_panel(title="Error Trends", y=11),
            timeseries_panel(
                title="Error Rate by Type",
                expr=(
                    "sum by (error) (count_over_time("
                    f'{_base_pipeline()} | event_type=~".*_ERROR" | error != "" '
                    "[$__interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 12},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{error}}",
                decimals=0,
                description=(
                    "Error events per graph interval, by error reason. Shows "
                    "whether a reported problem is an isolated event or part "
                    "of a spike, and roughly when it started."
                ),
            ),
            bar_gauge_panel(
                title="Top Error Types (selected range)",
                expr=(
                    "topk(10, sum by (error) (count_over_time("
                    f'{_base_pipeline()} | event_type=~".*_ERROR" | error != "" '
                    "[$__range])))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 12},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{error}}",
                decimals=0,
            ),
            row_panel(title="Scope & Correlation", y=20),
            timeseries_panel(
                title="Failures by Realm",
                expr=(
                    "sum by (realm) (count_over_time("
                    f'{_base_pipeline()} | event_type=~".*_ERROR" '
                    "[$__interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 21},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{realm}}",
                decimals=0,
                description=(
                    "Which realm is actually affected -- one app, or "
                    "everything at once."
                ),
            ),
            bar_gauge_panel(
                title="Top Offending IPs (errors, selected range)",
                expr=(
                    "topk(10, sum by (ip) (count_over_time("
                    f'{_base_pipeline()} | event_type=~".*_ERROR" | ip != "" '
                    "[$__range])))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 21},
                datasource_ref=LOKI_DATASOURCE_REF,
                legend_format="{{ip}}",
                decimals=0,
                description=(
                    "Distinguishes one user having a bad day (a single IP, "
                    "a handful of errors) from a bot/scanner hammering one "
                    "endpoint (one IP, a large count)."
                ),
            ),
            row_panel(title="Identity Provider Health", y=29),
            timeseries_panel(
                title="Identity Provider Success vs Failure",
                queries=[
                    {
                        "expr": (
                            "sum by (identity_provider) (count_over_time("
                            f'{{{_SELECTOR}}} {_KC_EVENT_FILTER} |~ "$search" '
                            f"{_EXTRACT} {_NARROW} "
                            '| event_type=~"LOGIN|IDENTITY_PROVIDER_LOGIN" '
                            '| identity_provider != "" '
                            "[$__interval]))"
                        ),
                        "legend_format": "{{identity_provider}} - success",
                    },
                    {
                        "expr": (
                            "sum by (identity_provider) (count_over_time("
                            f'{{{_SELECTOR}}} {_KC_EVENT_FILTER} |~ "$search" '
                            f"{_EXTRACT} {_NARROW} "
                            '| event_type=~"LOGIN_ERROR|IDENTITY_PROVIDER_LOGIN_ERROR" '
                            '| identity_provider != "" '
                            "[$__interval]))"
                        ),
                        "legend_format": "{{identity_provider}} - failure",
                    },
                ],
                grid_pos={"h": 8, "w": 24, "x": 0, "y": 30},
                datasource_ref=LOKI_DATASOURCE_REF,
                decimals=0,
                description=(
                    "Per-IdP login success/failure counts -- directly "
                    "answers 'is SSO broken for touchstone' style reports."
                ),
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    bar_gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the Keycloak incident-lookup dashboard."""
    create_dashboard(
        "keycloak-incident-lookup-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, bar_gauge_panel, logs_panel, row_panel),
        resource_opts,
    )
