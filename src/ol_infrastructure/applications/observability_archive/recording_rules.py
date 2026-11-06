"""Recording rules for the 13-month trends the archive exists to answer.

Mimir reads raw samples over the whole query range and leans on the
store-gateway for anything past the ingesters, so a 13-month `rate()` over
hundreds of thousands of series does not finish. Each trend worth asking over
that range is pre-aggregated here instead: per-service RED from the OTLP
`http.server.duration` histograms (what Cloud's span metrics answer for 31
days), CPU and memory by namespace, pod counts, and how much telemetry each
cluster ships by signal.

The rules are PrometheusRule objects in the archive namespace. The operations
cluster's `alloy-metrics` collector syncs them into Mimir's ruler through the
`archive-metrics` destination's `rules` block (substructure/aws/eks/grafana.py),
selected by `MIMIR_RULES_LABEL`, so the rules are code in this repo and never
state inside Mimir. Rule names follow Prometheus's `level:metric:operation`
convention.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Resource, ResourceOptions

from ol_infrastructure.lib.ol_types import K8sGlobalLabels
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.telemetry_archive import MIMIR_RULES_LABEL

# The OTLP http.server.duration histogram as Grafana Cloud and the archive both
# name it (Mimir's otel_metric_suffixes_enabled, see mimir.py). The grouping
# matches what otel_service_red.py and dashboards/service_red.py select on.
_DURATION = "http_server_duration_milliseconds"
_SERVICE_BY = "cluster, deployment_environment, service_namespace, service_name"
_SERVICE_SELECTOR = 'service_name=~".+"'
_SERVICE_5XX_SELECTOR = f'{_SERVICE_SELECTOR}, http_status_code=~"5.."'
_CONTAINER_SELECTOR = 'container!="", container!="POD"'

RULE_GROUPS: list[dict[str, object]] = [
    {
        "name": "archive-http-server-red",
        "interval": "1m",
        "rules": [
            {
                "record": "service:http_server_requests:rate5m",
                "expr": (
                    f"sum by ({_SERVICE_BY}) "
                    f"(rate({_DURATION}_count{{{_SERVICE_SELECTOR}}}[5m]))"
                ),
            },
            {
                "record": "service:http_server_5xx:rate5m",
                "expr": (
                    f"sum by ({_SERVICE_BY}) "
                    f"(rate({_DURATION}_count{{{_SERVICE_5XX_SELECTOR}}}[5m]))"
                ),
            },
            {
                "record": f"service:{_DURATION}_sum:rate5m",
                "expr": (
                    f"sum by ({_SERVICE_BY}) "
                    f"(rate({_DURATION}_sum{{{_SERVICE_SELECTOR}}}[5m]))"
                ),
            },
            {
                # Keeps `le` so histogram_quantile() works over the recorded
                # series for any quantile, not one chosen now.
                "record": f"service:{_DURATION}_bucket:rate5m",
                "expr": (
                    f"sum by ({_SERVICE_BY}, le) "
                    f"(rate({_DURATION}_bucket{{{_SERVICE_SELECTOR}}}[5m]))"
                ),
            },
        ],
    },
    {
        "name": "archive-kubernetes-resources",
        "interval": "1m",
        "rules": [
            {
                "record": "namespace:container_cpu_usage_seconds:rate5m",
                "expr": (
                    "sum by (cluster, namespace) "
                    f"(rate(container_cpu_usage_seconds_total{{{_CONTAINER_SELECTOR}}}[5m]))"
                ),
            },
            {
                "record": "namespace:container_memory_working_set_bytes:sum",
                "expr": (
                    "sum by (cluster, namespace) "
                    f"(container_memory_working_set_bytes{{{_CONTAINER_SELECTOR}}})"
                ),
            },
            {
                "record": "namespace:kube_pod_info:count",
                "expr": "count by (cluster, namespace) (kube_pod_info)",
            },
            {
                "record": "cluster:kube_node_info:count",
                "expr": "count by (cluster) (kube_node_info)",
            },
        ],
    },
    {
        # What each cluster's collectors ship, by signal, from the Alloy
        # self-metrics the k8s-monitoring chart scrapes. `url` and `exporter`
        # tell the Cloud leg from the archive leg of the same collector.
        "name": "archive-telemetry-volume",
        "interval": "1m",
        "rules": [
            {
                "record": "cluster:prometheus_remote_storage_samples:rate5m",
                "expr": (
                    "sum by (cluster, url) "
                    "(rate(prometheus_remote_storage_samples_total[5m]))"
                ),
            },
            {
                "record": "cluster:loki_write_sent_bytes:rate5m",
                "expr": (
                    "sum by (cluster, host) (rate(loki_write_sent_bytes_total[5m]))"
                ),
            },
            {
                "record": "cluster:otelcol_exporter_sent_log_records:rate5m",
                "expr": (
                    "sum by (cluster, exporter) "
                    "(rate(otelcol_exporter_sent_log_records_total[5m]))"
                ),
            },
            {
                "record": "cluster:otelcol_exporter_sent_metric_points:rate5m",
                "expr": (
                    "sum by (cluster, exporter) "
                    "(rate(otelcol_exporter_sent_metric_points_total[5m]))"
                ),
            },
            {
                "record": "cluster:otelcol_exporter_sent_spans:rate5m",
                "expr": (
                    "sum by (cluster, exporter) "
                    "(rate(otelcol_exporter_sent_spans_total[5m]))"
                ),
            },
        ],
    },
]


def build(
    *,
    stack_info: StackInfo,
    namespace: str,
    k8s_labels: K8sGlobalLabels,
    depends_on: list[Resource],
) -> kubernetes.apiextensions.CustomResource:
    """Declare the archive's recording rules as a PrometheusRule."""
    return kubernetes.apiextensions.CustomResource(
        f"telemetry-archive-recording-rules-{stack_info.env_suffix}",
        api_version="monitoring.coreos.com/v1",
        kind="PrometheusRule",
        metadata={
            "name": "telemetry-archive-recording-rules",
            "namespace": namespace,
            "labels": {**k8s_labels.model_dump(), **MIMIR_RULES_LABEL},
        },
        spec={"groups": RULE_GROUPS},
        opts=ResourceOptions(depends_on=depends_on),
    )
