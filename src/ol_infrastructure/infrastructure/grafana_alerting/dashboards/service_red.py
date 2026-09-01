"""RED metrics for the OTel-instrumented Django services, from the app's own meter.

Replaces two Grafana-Cloud-UI dashboards that both read trace-derived data and
neither of which works:

  `opentelemetry-apm` queried `duration_milliseconds_*`, the metric name a
  self-hosted otel-collector `spanmetrics` connector produces. Grafana Cloud's
  Tempo metrics-generator names its output `traces_spanmetrics_latency_*`
  instead, so every panel on that dashboard has always been empty here.

  `febljk0a32qyoa` ("Lightweight APM for OpenTelemetry") queries the *stable*
  semconv names -- `http_server_request_duration_seconds_*`,
  `http_client_request_duration_seconds_*`. Our SDK emits the old ones:
  `OTEL_SEMCONV_STABILITY_OPT_IN` is set nowhere in `src/`, and
  opentelemetry-instrumentation-wsgi/-asgi gate the stable metric behind it.
  So that dashboard is empty for our services too.

Even a spanmetrics dashboard with the right metric name would be misleading.
Tempo generates spanmetrics from what it *ingests*, i.e. after tail sampling,
so RED computed that way describes the sample rather than the traffic.
Measured 2026-09-01 against mitxonline-webapp, which had been emitting for 25h
(learn-webapp was mid-rollout and not yet a fair comparison), 1h rate windows:

  request rate   431/s from `http_server_duration_milliseconds_count`
                 1.05/s from `traces_spanmetrics_latency_count` -- 0.24%

The latency error is not a bias you could correct for, it is noise. Over the
same three hours, p95 on mitxonline-webapp:

  native, 1h window        357 - 401 ms
  native, 5m window        328 - 408 ms
  trace-derived, 1h        320 - 545 ms
  trace-derived, 5m        231 - 1246 ms

A 5.4x swing on the trace-derived estimate across a window where the native
measurement moved 11%, because at ~1 sampled span/s a 5m bucket holds a few
hundred spans and the tail sampler picked them for being slow or errored.
Reading it at one instant can show it 60% high or 40% low. That is why these
panels read the meter directly rather than applying a fudge factor.

`http_server_duration_milliseconds_*` bypasses the tail sampler entirely. In
the `grafana-k8s-monitoring-alloy-receiver` pipeline only `traces` route into
`otelcol.exporter.loadbalancing.gc_otlp_endpoint_sampler`; `metrics` go
straight to the OTLP exporter.

WHY `http_target` AND NOT `http_route`: the wsgi/asgi instrumentation carries
the Django URL *pattern* (`^api/v1/learning_resources/(?P<id>[^/.]+)/$`) on the
`http.target` metric attribute, and emits no `http.route` attribute at all.
So `http_target` is the endpoint dimension here, and it is a route pattern
rather than a raw path -- cardinality is 124 values for mitxonline-webapp and
65 for learn-webapp, not one per request.

WHAT IS ABSENT AND WHY: every edxapp workload sets `OTEL_METRICS_EXPORTER=none`
(applications/edxapp/k8s_resources.py), so LMS/CMS/celery emit traces but no
`http.server.duration` and will never appear in the `$service` list. That is by
design, not a gap in this dashboard.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Input, ResourceOptions

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.datasources import (
    MIMIR_DATASOURCE_REF,
)

_DURATION = "http_server_duration_milliseconds"
_SELECTOR = 'service_name=~"$service", http_target=~"$route"'
_5XX = f'{_SELECTOR}, http_status_code=~"5.."'
_4XX = f'{_SELECTOR}, http_status_code=~"4.."'

# `sum(...) or vector(0)` keeps an error-ratio panel reading 0 rather than "No
# data" during the (common, desirable) windows with no 5xx at all -- without it
# the numerator is an empty vector and the whole division returns nothing.
_ZERO = "or vector(0)"


def _rate(
    selector: str, suffix: str = "count", window: str = "$__rate_interval"
) -> str:
    return f"rate({_DURATION}_{suffix}{{{selector}}}[{window}])"


def _quantile(quantile: float, by: str = "") -> str:
    grouping = f"le, {by}" if by else "le"
    return (
        f"histogram_quantile({quantile}, "
        f"sum by ({grouping}) ({_rate(_SELECTOR, 'bucket')}))"
    )


def _dashboard_json(
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": "otel-service-red",
        "title": "Service RED - OpenTelemetry",
        "description": (
            "Rate, errors and duration for the OTel-instrumented Django "
            "services, read from each app's own MeterProvider "
            "(http.server.duration) rather than from Tempo-derived "
            "spanmetrics. Metrics bypass the tail sampler, so these numbers "
            "describe all traffic instead of the sampled fraction."
        ),
        "tags": ["opentelemetry", "apm", "red"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "templating": {
            "list": [
                {
                    "name": "service",
                    "label": "Service",
                    "type": "query",
                    "datasource": MIMIR_DATASOURCE_REF,
                    "query": f"label_values({_DURATION}_count, service_name)",
                    "multi": True,
                    "includeAll": True,
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                },
                {
                    "name": "route",
                    "label": "Endpoint pattern",
                    "type": "textbox",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
            ]
        },
        "panels": [
            row_panel(title="Overview", y=0),
            stat_panel(
                title="Request rate",
                expr=f"sum({_rate(_SELECTOR)})",
                grid_pos={"h": 4, "w": 6, "x": 0, "y": 1},
                unit="reqps",
                decimals=1,
            ),
            stat_panel(
                title="5xx rate",
                expr=f"(sum({_rate(_5XX)}) {_ZERO}) / sum({_rate(_SELECTOR)})",
                grid_pos={"h": 4, "w": 6, "x": 6, "y": 1},
                unit="percentunit",
                decimals=2,
            ),
            stat_panel(
                title="p95 latency",
                expr=_quantile(0.95),
                grid_pos={"h": 4, "w": 6, "x": 12, "y": 1},
                unit="ms",
                decimals=0,
            ),
            stat_panel(
                title="In-flight requests",
                expr='sum(http_server_active_requests{service_name=~"$service"})',
                grid_pos={"h": 4, "w": 6, "x": 18, "y": 1},
                unit="short",
                decimals=0,
            ),
            row_panel(title="Rate", y=5),
            timeseries_panel(
                title="Requests per second by service",
                expr=f"sum by (service_name) ({_rate(_SELECTOR)})",
                legend_format="{{service_name}}",
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 6},
                unit="reqps",
                legend_calc="mean",
            ),
            timeseries_panel(
                title="Requests per second by status code",
                expr=f"sum by (http_status_code) ({_rate(_SELECTOR)})",
                legend_format="{{http_status_code}}",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 6},
                unit="reqps",
                legend_calc="mean",
            ),
            row_panel(title="Errors", y=14),
            timeseries_panel(
                title="Error ratio",
                queries=[
                    {
                        "expr": (
                            f"(sum({_rate(_5XX)}) {_ZERO}) / sum({_rate(_SELECTOR)})"
                        ),
                        "legend_format": "5xx",
                    },
                    {
                        "expr": (
                            f"(sum({_rate(_4XX)}) {_ZERO}) / sum({_rate(_SELECTOR)})"
                        ),
                        "legend_format": "4xx",
                    },
                ],
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 15},
                unit="percentunit",
                description=(
                    "4xx is graphed alongside 5xx because a large share of "
                    "these services' 4xx are 401/403 from unauthenticated "
                    "API polling, which is normal traffic rather than a "
                    "fault -- a 4xx spike is worth reading, a steady 4xx "
                    "floor is not."
                ),
            ),
            timeseries_panel(
                title="Top endpoints by 5xx rate",
                expr=f"topk(10, sum by (http_target) ({_rate(_5XX)}))",
                legend_format="{{http_target}}",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 15},
                unit="reqps",
                legend_calc="max",
            ),
            row_panel(title="Duration", y=23),
            timeseries_panel(
                title="Latency percentiles",
                queries=[
                    {"expr": _quantile(0.50), "legend_format": "p50"},
                    {"expr": _quantile(0.95), "legend_format": "p95"},
                    {"expr": _quantile(0.99), "legend_format": "p99"},
                ],
                grid_pos={"h": 8, "w": 12, "x": 0, "y": 24},
                unit="ms",
                legend_calc="max",
                description=(
                    "Interpolated from the SDK's default 15 explicit bucket "
                    "boundaries (0,5,10,25,50,75,100,250,500,750,1000,2500,"
                    "5000,7500,10000 ms). A p99 sitting inside the 2500-5000 "
                    "bucket is precise to that bucket and no further."
                ),
            ),
            timeseries_panel(
                title="Top endpoints by p95 latency",
                expr=f"topk(10, {_quantile(0.95, by='http_target')})",
                legend_format="{{http_target}}",
                grid_pos={"h": 8, "w": 12, "x": 12, "y": 24},
                unit="ms",
                legend_calc="max",
                description=(
                    "Ranks on latency alone, so a once-an-hour endpoint can "
                    "outrank a hot one. Read it next to the Endpoints table "
                    "below, which carries the request rate."
                ),
            ),
            row_panel(title="Endpoints", y=32),
            table_panel(
                title="Per-endpoint RED",
                join_field="http_target",
                sort_by="Requests/sec",
                columns=[
                    {
                        "expr": f"sum by (http_target) ({_rate(_SELECTOR)})",
                        "title": "Requests/sec",
                        "unit": "reqps",
                    },
                    {
                        "expr": _quantile(0.95, by="http_target"),
                        "title": "p95 (ms)",
                        "unit": "ms",
                    },
                    {
                        # No `or vector(0)` here, unlike the panels above:
                        # `vector(0)` carries no labels, so it can never match
                        # a `by (http_target)` denominator. An endpoint with no
                        # 5xx correctly leaves this cell empty instead.
                        "expr": (
                            f"sum by (http_target) ({_rate(_5XX)}) / "
                            f"sum by (http_target) ({_rate(_SELECTOR)})"
                        ),
                        "title": "5xx ratio",
                        "unit": "percentunit",
                    },
                ],
                grid_pos={"h": 12, "w": 24, "x": 0, "y": 33},
                description=(
                    "One row per Django URL pattern. `http_target` holds the "
                    "route regex, not the requested path, so this stays at "
                    "roughly 100 rows per service rather than growing with "
                    "traffic."
                ),
            ),
            row_panel(title="Saturation", y=45),
            timeseries_panel(
                title="In-flight requests by pod",
                expr=(
                    "sum by (k8s_pod_name) "
                    '(http_server_active_requests{service_name=~"$service"})'
                ),
                legend_format="{{k8s_pod_name}}",
                grid_pos={"h": 8, "w": 24, "x": 0, "y": 46},
                unit="short",
                legend_calc="max",
                description=(
                    "The $route filter does not apply here: "
                    "http_server_active_requests carries no http_target "
                    "attribute, only method/scheme/host. Compare the peak "
                    "against the pod's worker+thread budget to see whether "
                    "requests are queueing."
                ),
            ),
        ],
    }


def create(
    folder_uid: Input[str],
    timeseries_panel: Callable[..., dict[str, Any]],
    stat_panel: Callable[..., dict[str, Any]],
    table_panel: Callable[..., dict[str, Any]],
    row_panel: Callable[..., dict[str, Any]],
    create_dashboard: Callable[
        [str, Input[str], dict[str, Any], ResourceOptions], None
    ],
    resource_opts: ResourceOptions,
) -> None:
    """Create the OpenTelemetry service RED dashboard."""
    create_dashboard(
        "otel-service-red-dashboard",
        folder_uid,
        _dashboard_json(timeseries_panel, stat_panel, table_panel, row_panel),
        resource_opts,
    )
