"""OpenTelemetry + structured-logging environment for the witan workloads.

agent-kit ``witan_core.observability`` (PR #193) installs structlog plus OTel
tracer and meter providers, but it deliberately installs **no provider at all**
when no OTLP endpoint is in the environment — that is what keeps every local
CLI run and stdio session free of exporters. So the instrumentation is inert
until something sets these variables, and this module is that something.

Nothing here is witan-specific translation: the code passes no endpoint to the
exporters and no service name to the ``Resource``, precisely so the OTel SDK
reads the standard variables itself. Adding a witan-shaped indirection layer
would be a second thing to keep in sync with the SDK for no gain.

── Why CI gets logs but no traces or metrics ──
``setup_grafana`` (``substructure/aws/eks/grafana.py``) returns early for CI, so
operations-ci runs no Grafana Alloy: its ``grafana`` namespace exists and is
empty, and ``grafana-k8s-monitoring-alloy-receiver`` — the Service every
precedent exports to — resolves in QA and Production only. Setting the endpoint
uniformly across all three stacks would therefore not be free in CI; it would
point the exporters at a name that does not resolve and buy a connection
failure per batch and per 60s metric interval, forever, in the one environment
where nobody is watching. ``ships_telemetry`` mirrors ``setup_grafana``'s own
condition instead, so CI lands back on the SDK's no-provider path by
construction.

CI still gets the structured JSON logs, which are the half that does not depend
on Alloy at all: they go to stderr and the k8s-monitoring chart ships pod logs
to Loki. That is why ``witan_log_env`` is unconditional while the OTel block is
not.

── There is no longer a tier in front of witan (2026-08-15) ──
Until this date two ToolHive hops sat between APISIX and witan — the
``VirtualMCPServer`` aggregator and the ``MCPServer`` proxyrunner — each a
separate Go binary with its own OTel pipeline, configured through
``MCPTelemetryConfig`` CRs rather than these variables. Instrumenting them was
what finally answered "where did the 20s go": witan's own ``duration_ms``
starts INSIDE its middleware, so the ~2.5s a request spent being forwarded to
it under load was invisible to every signal either side measured.

That investigation is what justified removing them. The answer was that the
tier's pre-handler interval consumed 8.2% of the very 30s deadline the tier was
enforcing, and that the deadline fired while witan was still committing a write
the caller was then told had failed. With the hops gone, the interval they hid
is gone with them, and what this module configures is once again the whole
story of a request.

So there is nothing to reconcile any more: witan's spans start at the ASGI
boundary, and the only hop in front is APISIX, which reports its own timings
under ``service.name: apisix``. Anyone chasing latency should compare those two
rather than looking for a ToolHive histogram that no longer exists.
"""

import pulumi_kubernetes as kubernetes

from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.toolhive_telemetry import (
    OTLP_ENDPOINT,
    ships_telemetry,
)

# Sample everything here; Alloy's `tailSampling` does the filtering (keep
# errors, keep >5000ms, 15% of the rest — substructure/aws/eks/grafana.py).
# That is the house pattern, stated in components/services/apisix.py: the
# gateway runs `always_on` for the same reason. witan was the outlier.
#
# ★ THIS MOSTLY AFFECTS PARENTLESS TRACES, WHICH IS NARROWER THAN IT LOOKS.
# APISIX runs `always_on` and propagates a sampled `traceparent`, and
# `parentbased_*` honours a parent's decision — so anything arriving through
# the gateway was already sampled at 100%. The old 0.25 ratio only ever applied
# to traces witan roots itself: CronJobs, the CI indexer, migration Jobs.
#
# The rationale this replaces was "honour whatever ToolHive decided at the
# root". ToolHive was removed in #5448, so that decision-maker is gone; APISIX
# is the parent now, and `parentbased_always_on` still respects an inbound
# "not sampled" so a cross-service trace stays coherent.
TRACES_SAMPLER = "parentbased_always_on"
TRACES_SAMPLER_ARG = "1.0"
PROPAGATORS = "tracecontext,baggage"
METRIC_EXPORT_INTERVAL_MS = "60000"

# The three variables `witan_core.observability` reads for pod identity. It adds
# them BOTH as OTel resource attributes (`k8s.pod.name`, `k8s.namespace.name`,
# `k8s.node.name`) and as log fields (`pod_name`, `namespace`, `node_name`), and
# it reads them ONCE at import — so they have to be in the pod environment at
# startup, which is what makes the downward API the only workable source.
# Without them a span cannot be attributed to a replica.
_DOWNWARD_API_FIELDS = {
    "KUBERNETES_POD_NAME": "metadata.name",
    "KUBERNETES_NAMESPACE": "metadata.namespace",
    "KUBERNETES_NODE_NAME": "spec.nodeName",
}


def witan_log_env() -> dict[str, str]:
    """Structured-logging variables, safe in every environment.

    Both are what the code already defaults to in a container (``json``
    whenever stderr is not a tty, ``INFO`` when neither ``WITAN_LOG_LEVEL`` nor
    ``LOG_LEVEL`` is set). Stated anyway: a log format that depends on tty
    detection is one ``kubectl exec`` away from looking different from what the
    Loki pipeline was built against.
    """
    return {
        "WITAN_LOG_FORMAT": "json",
        "WITAN_LOG_LEVEL": "INFO",
    }


def otel_env(
    stack_info: StackInfo,
    service_name: str,
    service_version: str,
) -> dict[str, str]:
    """Build the standard ``OTEL_*`` variables for a witan workload.

    Returns an empty mapping where there is no collector, which puts
    ``configure_tracing()``/``configure_metrics()`` back on their intended
    no-provider path rather than on a failing exporter.

    :param service_name: e.g. ``"witan"`` — prefixed with the environment to
        match the ``<env>-<service>`` convention edxapp and mit_learn use.
    :param service_version: the image tag or digest this workload runs, from
        ``get_docker_image_tag("WITAN")``. The precedents carry a literal
        ``${GIT_SHA:-unknown}`` because their Helm values are expanded by an
        entrypoint shell; nothing expands variables here, so a copied literal
        would ship the string ``${GIT_SHA:-unknown}`` as the version. Pulumi
        already knows the exact identifier the pod will run, so use it.
    """
    if not ships_telemetry(stack_info):
        return {}

    resource_attributes = ",".join(
        [
            f"deployment.environment={stack_info.env_suffix}",
            "service.namespace=witan",
            f"service.version={service_version}",
        ]
    )
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP_ENDPOINT,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_SERVICE_NAME": f"{stack_info.env_suffix}-{service_name}",
        "OTEL_RESOURCE_ATTRIBUTES": resource_attributes,
        "OTEL_TRACES_SAMPLER": TRACES_SAMPLER,
        "OTEL_TRACES_SAMPLER_ARG": TRACES_SAMPLER_ARG,
        "OTEL_PROPAGATORS": PROPAGATORS,
        "OTEL_METRIC_EXPORT_INTERVAL": METRIC_EXPORT_INTERVAL_MS,
    }


def downward_api_env_args() -> list[kubernetes.core.v1.EnvVarArgs]:
    """Pod-identity env as ``EnvVarArgs``, for ordinary Job/CronJob specs."""
    return [
        kubernetes.core.v1.EnvVarArgs(
            name=name,
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                field_ref=kubernetes.core.v1.ObjectFieldSelectorArgs(
                    field_path=field_path,
                )
            ),
        )
        for name, field_path in _DOWNWARD_API_FIELDS.items()
    ]
