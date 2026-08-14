"""General service-health overview for Keycloak, across all realms.

Backed by Keycloak's built-in Micrometer metrics (`metrics-enabled: true` in
applications/keycloak/__main__.py, scraped via the operator's auto-created
ServiceMonitor) plus a couple of raw-log panels for warning/error tails.
Portable across QA and Production: every query is scoped to the fixed
`namespace="keycloak"` label rather than a hardcoded cluster/environment, and
the `$realm` template variable defaults to all realms.

This is the Pulumi port of the Grafana-Cloud-UI-authored "Keycloak --
Production Overview" dashboard, generalized for QA as well as Production,
with a handful of panels folded in from a now-retired CI-only dashboard
("Keycloak troubleshooting dashboard") that covered ground this one didn't:
GC pause time/count, JDBC (Infinispan) cache hit ratio, and an availability
SLO gauge.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    LOKI_DATASOURCE_REF,
)

_NAMESPACE_SELECTOR = 'namespace="keycloak"'


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "keycloak-overview",
        "title": "Keycloak - Overview",
        "description": (
            "General service-health overview for Keycloak: logins, JVM, "
            "HTTP, and DB connection pool, across all realms."
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
                    "name": "realm",
                    "type": "query",
                    "datasource": {
                        "type": "prometheus",
                        "uid": "grafanacloud-prom",
                    },
                    "query": f"label_values(keycloak_user_events_total{{{_NAMESPACE_SELECTOR}}},realm)",
                    "multi": True,
                    "includeAll": True,
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                },
                {
                    "name": "cluster",
                    "type": "query",
                    "datasource": LOKI_DATASOURCE_REF,
                    "query": f"label_values({{{_NAMESPACE_SELECTOR}}},cluster)",
                    "multi": True,
                    "includeAll": True,
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                },
            ]
        },
        "panels": [
            row_panel(title="", y=0),
            stat_panel(
                title="Pods Ready",
                expr=f'count(up{{{_NAMESPACE_SELECTOR}, container="keycloak"}} == 1)',
                grid_pos={"h": 4, "w": 4, "x": 0, "y": 1},
            ),
            gauge_panel(
                title="Availability (SLO)",
                expr=(
                    f"count_over_time(sum(up{{{_NAMESPACE_SELECTOR}, "
                    'container="keycloak"} > 0)[$__range:$__interval]) '
                    "/\n"
                    "count_over_time(vector(1)[$__range:$__interval])"
                ),
                grid_pos={"h": 4, "w": 4, "x": 4, "y": 1},
            ),
            stat_panel(
                title="Logins (selected range)",
                expr=(
                    f'sum(increase(keycloak_user_events_total{{event="login", error="", '
                    f'{_NAMESPACE_SELECTOR}, realm=~"$realm"}}[$__range]))'
                ),
                grid_pos={"h": 4, "w": 4, "x": 8, "y": 1},
            ),
            stat_panel(
                title="Login Error %",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="login", error!="", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__range]))\n/\n"
                    "sum(increase(keycloak_user_events_total"
                    f'{{event="login", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__range]))"
                ),
                grid_pos={"h": 4, "w": 4, "x": 12, "y": 1},
                unit="percentunit",
            ),
            stat_panel(
                title="Account Lockouts (selected range)",
                expr=(
                    "sum(increase(keycloak_user_events_total"
                    f'{{error=~"user_disabled|user_temporarily_disabled", {_NAMESPACE_SELECTOR}, '
                    'realm=~"$realm"}[$__range]))'
                ),
                grid_pos={"h": 4, "w": 4, "x": 16, "y": 1},
            ),
            stat_panel(
                title="Average GC Pause Time",
                expr=(
                    f"sum(rate(jvm_gc_pause_seconds_sum{{{_NAMESPACE_SELECTOR}}}[$__range]))"
                    "\n/\n"
                    f"sum(rate(jvm_gc_pause_seconds_count{{{_NAMESPACE_SELECTOR}}}[$__range]))"
                ),
                grid_pos={"h": 4, "w": 4, "x": 20, "y": 1},
                unit="s",
            ),
            timeseries_panel(
                title="Logins/min",
                expr=(
                    "sum(rate(keycloak_user_events_total"
                    f'{{event="login", error="", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__rate_interval])) * 60"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 5},
                legend_format="logins/min",
            ),
            timeseries_panel(
                title="Login Failures/min",
                expr=(
                    "sum(rate(keycloak_user_events_total"
                    f'{{event="login", error!="", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__rate_interval])) * 60"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 5},
                legend_format="failures/min",
            ),
            row_panel(title="Authentication Events", y=13),
            timeseries_panel(
                title="Successful Logins by Realm",
                expr=(
                    "sum by (realm) (rate(keycloak_user_events_total"
                    f'{{event="login", error="", {_NAMESPACE_SELECTOR}, '
                    'realm=~"$realm"}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 14},
                legend_format="{{realm}}",
            ),
            timeseries_panel(
                title="Login Errors by Type",
                expr=(
                    "sum by (error) (rate(keycloak_user_events_total"
                    f'{{event="login", error!="", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 14},
                legend_format="{{error}}",
            ),
            timeseries_panel(
                title="Login & Logout Events by Realm",
                expr=(
                    "sum by (realm, event) (rate(keycloak_user_events_total"
                    f'{{event=~"login|logout", {_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 22},
                legend_format="{{realm}} - {{event}}",
            ),
            timeseries_panel(
                title="Token Operations & Registrations",
                expr=(
                    "sum by (event) (rate(keycloak_user_events_total"
                    '{event=~"refresh_token|code_to_token|register|token_exchange", '
                    f'{_NAMESPACE_SELECTOR}, realm=~"$realm"}}'
                    "[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 22},
                legend_format="{{event}}",
            ),
            row_panel(title="HTTP Performance", y=30),
            timeseries_panel(
                title="HTTP Request Rate by Status",
                expr=(
                    "sum by (status) (rate(http_server_requests_seconds_count"
                    f"{{{_NAMESPACE_SELECTOR}}}[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 31},
                legend_format="{{status}}",
            ),
            timeseries_panel(
                # Keycloak's Micrometer HTTP timer is exposed as a summary
                # (_count/_sum/_max), not a histogram -- there's no _bucket
                # series, so histogram_quantile() has nothing to compute
                # against. _max is Micrometer's own decaying max over its
                # publishing window, the closest available substitute for a
                # true percentile.
                title="HTTP Request Latency (avg/max)",
                queries=[
                    {
                        "expr": (
                            "sum(rate(http_server_requests_seconds_sum"
                            f"{{{_NAMESPACE_SELECTOR}}}[$__rate_interval]))"
                            "\n/\n"
                            "sum(rate(http_server_requests_seconds_count"
                            f"{{{_NAMESPACE_SELECTOR}}}[$__rate_interval]))"
                        ),
                        "legend_format": "avg",
                    },
                    {
                        "expr": (
                            "max(http_server_requests_seconds_max"
                            f"{{{_NAMESPACE_SELECTOR}}})"
                        ),
                        "legend_format": "max",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 31},
                unit="s",
            ),
            timeseries_panel(
                title="HTTP Error Rate",
                expr=(
                    "sum(rate(http_server_requests_seconds_count"
                    f'{{{_NAMESPACE_SELECTOR}, status=~"5.."}}[$__rate_interval]))'
                    "\n/\n"
                    "sum(rate(http_server_requests_seconds_count"
                    f"{{{_NAMESPACE_SELECTOR}}}[$__rate_interval]))"
                ),
                grid_pos={"h": 8, "w": 24, "x": 0, "y": 39},
                legend_format="5xx rate",
                unit="percentunit",
            ),
            row_panel(title="JVM & Database", y=47),
            timeseries_panel(
                title="JVM Heap Memory",
                queries=[
                    {
                        "expr": (
                            "sum by (pod) (jvm_memory_used_bytes"
                            f'{{{_NAMESPACE_SELECTOR}, area="heap"}})'
                        ),
                        "legend_format": "used - {{pod}}",
                    },
                    {
                        "expr": (
                            "sum by (pod) (jvm_memory_max_bytes"
                            f'{{{_NAMESPACE_SELECTOR}, area="heap"}})'
                        ),
                        "legend_format": "max - {{pod}}",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 48},
                unit="bytes",
                legend_calc="max",
            ),
            timeseries_panel(
                title="CPU Usage per Pod",
                expr=f"process_cpu_usage{{{_NAMESPACE_SELECTOR}}}",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 48},
                legend_format="{{pod}}",
                unit="percentunit",
            ),
            timeseries_panel(
                title="DB Connection Pool (Agroal)",
                queries=[
                    {
                        "expr": f"sum(agroal_active_count{{{_NAMESPACE_SELECTOR}}})",
                        "legend_format": "active",
                    },
                    {
                        "expr": f"sum(agroal_available_count{{{_NAMESPACE_SELECTOR}}})",
                        "legend_format": "available",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 56},
            ),
            timeseries_panel(
                title="GC Pause Time by Cause",
                expr=(
                    "sum by (cause) (irate(jvm_gc_pause_seconds_sum"
                    f"{{{_NAMESPACE_SELECTOR}}}[5m]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 56},
                legend_format="{{cause}}",
                unit="s",
            ),
            timeseries_panel(
                title="GC Events by Cause",
                expr=(
                    "sum by (cause) (irate(jvm_gc_pause_seconds_count"
                    f"{{{_NAMESPACE_SELECTOR}}}[5m]))"
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 64},
                legend_format="{{cause}}",
            ),
            timeseries_panel(
                title="JDBC Cache Hit Ratio",
                expr=f"avg(vendor_statistics_hit_ratio{{{_NAMESPACE_SELECTOR}}})",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 64},
                legend_format="hit ratio (all caches)",
                unit="percentunit",
            ),
            row_panel(title="Logs", y=72),
            logs_panel(
                title="Error & Warning Logs",
                expr=(
                    f'{{{_NAMESPACE_SELECTOR}, container="keycloak", '
                    'cluster=~"$cluster"} |~ "WARN|ERROR"'
                ),
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 73},
            ),
            logs_panel(
                title="Auth Event Failures",
                expr=(
                    f'{{{_NAMESPACE_SELECTOR}, container="keycloak", '
                    'cluster=~"$cluster"} '
                    '|~ `type="(LOGIN_ERROR|REGISTER_ERROR|'
                    'IDENTITY_PROVIDER_LOGIN_ERROR)"`'
                ),
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 73},
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    gauge_panel: Callable[..., dict[str, Any]],
    logs_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the general Keycloak service-health overview dashboard."""
    create_dashboard(
        "keycloak-overview-dashboard",
        folder_uid,
        _dashboard_json(
            timeseries_panel,
            stat_panel,
            gauge_panel,
            logs_panel,
            row_panel,
        ),
        resource_opts,
    )
