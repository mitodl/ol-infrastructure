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

── The ToolHive tier (``toolhive_*`` below) ──
Everything above configures the *witan process*. The two ToolHive hops in front
of it — the ``VirtualMCPServer`` aggregator and the ``MCPServer`` proxyrunner —
are separate Go binaries with their own OTel pipeline, configured through an
``MCPTelemetryConfig`` CR rather than environment variables. They were dark
until 2026-08-14 and are why the question "where did the 20s go" had no answer.

That gap is not academic. witan's own ``duration_ms`` starts INSIDE its
middleware; ToolHive's ``toolhive_mcp_request_duration_seconds`` wraps
``next.ServeHTTP`` (``pkg/telemetry/middleware.go``). On 2026-08-14 a
concurrency probe run saw the store finish all 8 writes at a normal 12.8s
median while the client was told 7 of them failed — time spent in an interval
NEITHER tier measured.

★ WHAT THE METRIC DELTA IS, AND IS NOT. ToolHive's timer starts just before it
forwards and stops after the downstream response returns, so it spans
``pre-forward + witan + post-response``. Subtracting witan's ``duration_ms``
therefore yields the TOTAL TIME OUTSIDE witan's middleware — both outer
intervals summed — not the pre-handler interval on its own. It also cannot be
done per request: the ToolHive side is a histogram and the witan side is a log
field, so they pair only in aggregate (percentile against percentile).

The pre-handler interval specifically needs the SPANS, where ToolHive's and
witan's are nested with real start timestamps and the two boundaries are
separable per request. That is why tracing is enabled here and not just
metrics, and why the probe runs in QA — the metric delta alone would say only
that time went somewhere outside witan, which is already known.
"""

import pulumi_kubernetes as kubernetes

from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.toolhive_telemetry import (
    DEFAULT_TRACE_SAMPLING_RATE,
    OTLP_ENDPOINT,
    ships_telemetry,
)

# Sampling and propagation, matched to the mit_learn/learn_ai precedent rather
# than chosen fresh, so a trace that crosses from one of those services into
# witan is sampled consistently instead of being decided twice.
#
# TRACES_SAMPLER_ARG is the SHARED default rather than a second literal: witan
# runs `parentbased_traceidratio`, so it honours whatever ToolHive decided at
# the root, and the two drifting apart would mean the ratio witan declares is
# not the ratio it gets. See `toolhive_trace_sampling_rate`.
TRACES_SAMPLER = "parentbased_traceidratio"
TRACES_SAMPLER_ARG = DEFAULT_TRACE_SAMPLING_RATE
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


def downward_api_env_dicts() -> list[dict[str, object]]:
    """Pod-identity env in plain-dict form, for a ``podTemplateSpec`` patch.

    The ``MCPServer`` CRD's own ``spec.env`` is name/value only, so a
    ``fieldRef`` cannot go there; ToolHive's PodTemplateSpec escape hatch takes
    a full ``EnvVar`` and merges it onto the operator-managed ``mcp`` container
    — which is already how ``spec.secrets`` reaches the pod as
    ``secretKeyRef`` entries.

    THIS DOES NOT CLOBBER THE OPERATOR'S OWN ENV, and it is worth stating
    because the obvious worry — that a PodTemplateSpec patch replaces list
    fields wholesale, RFC 7386 style — is a real failure mode for other CRDs
    and simply is not how this path works. Traced through toolhive v0.40.1,
    every step is an explicit append:

    1. ``PodTemplateSpecBuilder.WithSecrets`` starts from *this* template,
       finds the container named ``mcp``, and does
       ``Env = append(Env, secretEnvVars...)`` — the vars below are the
       existing ``Env``, the secrets land after them. The result is what the
       operator serializes into ``--k8s-pod-patch``.
    2. The runner applies that patch to an **empty** base PodTemplateSpec, so
       its ``WithSpec`` whole-spec assignment has nothing to overwrite.
    3. ``configureContainer`` then calls client-go's
       ``ContainerApplyConfiguration.WithEnv``, itself an append, to add the
       ``spec.env`` entries.

    Final order is podTemplateSpec env, then ``spec.secrets``, then
    ``spec.env`` — all three coexist, which the live workload confirms
    (2 ``secretKeyRef`` vars from the patch alongside 8 ``spec.env`` vars).
    The one real constraint is that concatenation does not de-duplicate, so a
    name used here must not collide with a ``spec.env`` or ``spec.secrets``
    name; the three below do not.
    """
    return [
        {
            "name": name,
            "valueFrom": {"fieldRef": {"fieldPath": field_path}},
        }
        for name, field_path in _DOWNWARD_API_FIELDS.items()
    ]


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
