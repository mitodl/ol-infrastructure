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

# The in-cluster OTLP/HTTP receiver, shared with mit_learn, learn_ai and edxapp
# (see their Pulumi.{QA,Production}.yaml and edxapp/k8s_configmaps.py). Port
# 4318 is http/protobuf; 4317 on the same Service is gRPC, which we do not use.
OTLP_ENDPOINT = (
    "http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318"
)

# Sampling and propagation, matched to the mit_learn/learn_ai precedent rather
# than chosen fresh, so a trace that crosses from one of those services into
# witan is sampled consistently instead of being decided twice.
TRACES_SAMPLER = "parentbased_traceidratio"
TRACES_SAMPLER_ARG = "0.25"
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


def ships_telemetry(stack_info: StackInfo) -> bool:
    """Whether this environment has an OTLP receiver to export to.

    Mirrors ``setup_grafana``'s CI early-return. Kept as a named predicate so
    the reason a stack is dark is one grep away from the reason the collector
    is absent.

    The ``.lower()`` is redundant and deliberate: ``parse_stack`` builds
    ``env_suffix`` as ``stack_name.lower()`` (lib/pulumi_helper.py), so it is
    lowercase by construction and the telemetry labels below can use it raw
    without risk of a ``QA``/``qa`` split. It is kept here only so this
    predicate is character-for-character the condition ``setup_grafana``
    tests — the two drifting apart is the failure this function exists to
    prevent.
    """
    return stack_info.env_suffix.lower() != "ci"


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


#########################################
#   The ToolHive tier                    #
#########################################
# One MCPTelemetryConfig per hop, NOT one shared between them. The two differ in
# exactly one field — `prometheus.enabled` — and that difference is a security
# boundary, not a preference. See `expose_prometheus` on
# `toolhive_telemetry_spec`.
TOOLHIVE_TELEMETRY_BACKEND_CONFIG_NAME = "witan-telemetry-backend"
TOOLHIVE_TELEMETRY_VMCP_CONFIG_NAME = "witan-telemetry-vmcp"


def toolhive_service_name(stack_info: StackInfo, component: str) -> str:
    """Per-hop OTel service name, `<env>-witan-<component>`.

    ToolHive's own CRD documentation requires this to be unique per server, and
    it is the only thing that separates the aggregator's spans from the
    proxy's. Same `<env>-<service>` convention `otel_env` uses, so all three
    witan-related services sort together in Grafana.
    """
    return f"{stack_info.env_suffix}-witan-{component}"


def toolhive_trace_sampling_rate(stack_info: StackInfo) -> str:
    """Head sampling rate for the ToolHive hops.

    ★ THIS IS THE ROOT SAMPLING DECISION FOR THE WHOLE REQUEST. ToolHive
    receives the request first, so it starts the trace; witan_core runs
    `parentbased_traceidratio` (TRACES_SAMPLER above), which honours an
    incoming decision rather than re-rolling it. So this value — not
    TRACES_SAMPLER_ARG — sets what fraction of end-to-end traces exist.

    QA samples everything because QA is where the concurrency probe runs and a
    sampled-away trace is a probe run that measured nothing. It is affordable
    precisely because QA carries no organic traffic: the probe's own ~50
    requests per run are essentially the entire span volume.

    Production keeps the 0.25 the other services use. Raising it there would
    change span cost against real traffic, which is a billing decision and not
    this module's to make.
    """
    return "1.0" if stack_info.env_suffix.lower() == "qa" else TRACES_SAMPLER_ARG


def toolhive_telemetry_spec(
    stack_info: StackInfo,
    component: str,
    *,
    expose_prometheus: bool,
) -> dict[str, object] | None:
    """Build an ``MCPTelemetryConfigSpec``, or ``None`` if there is nothing to enable.

    Returns ``None`` rather than an all-disabled spec so CI does not carry a CR
    that configures nothing — a referenced-but-inert config is the kind of thing
    that reads as "telemetry is on" to the next person to look.

    :param expose_prometheus: whether to serve the Prometheus ``/metrics`` path.

        ★ FALSE FOR THE vMCP, AND THAT IS LOAD-BEARING. ToolHive serves
        ``/metrics`` on the *main transport port* — there is no separate admin
        listener (toolhive `pkg/telemetry/config.go`: "The metrics are served on
        the main transport port at /metrics"). The vMCP's port 4483 is the one
        APISIX publishes, and `ingress.py` routes `paths=["/*"]` at it, so
        enabling the path there would put an unauthenticated
        ``https://<vmcp-host>/metrics`` on the public internet, listing every
        tool name and its call counts. The backend proxy's 8080 is ClusterIP
        only and never routed, so it is safe there.

        Revisit only alongside a path-level deny in the APISIX route — not by
        flipping this flag.

    The Prometheus path is enabled in EVERY environment including CI, unlike
    OTLP. It costs one HTTP route on a port already listening and opens no
    outbound connection, so `ships_telemetry`'s reasoning does not apply to it.
    It is also the only read available in CI, where the probe currently runs:
    port-forward, scrape before and after a run, and diff the cumulative
    histograms. That yields a window bounded on BOTH sides by construction,
    which `kubectl logs --since` (no `--until`) cannot do — an unbounded window
    silently folds in everything up to the moment of asking, which produced
    three wrong conclusions during the 2026-08-14 analysis.
    """
    otlp_enabled = ships_telemetry(stack_info)
    if not (otlp_enabled or expose_prometheus):
        return None

    spec: dict[str, object] = {"prometheus": {"enabled": expose_prometheus}}
    if otlp_enabled:
        spec["openTelemetry"] = {
            "enabled": True,
            # Scheme-less by the time ToolHive uses it — `NormalizeTelemetryConfig`
            # strips http(s):// because the OTLP client wants host:port — but
            # passed whole so this and OTEL_EXPORTER_OTLP_ENDPOINT above are
            # visibly the same endpoint. `insecure` is what actually selects
            # plaintext, and must stay in step with OTLP_ENDPOINT's scheme.
            "endpoint": OTLP_ENDPOINT,
            "insecure": True,
            "metrics": {"enabled": True},
            "tracing": {
                "enabled": True,
                "samplingRate": toolhive_trace_sampling_rate(stack_info),
            },
            # service.name is NOT set here — it comes from the per-hop
            # `telemetryConfigRef.serviceName` override, which is what lets one
            # spec shape serve two differently-named hops.
            "resourceAttributes": {
                "deployment.environment": stack_info.env_suffix,
                "service.namespace": "witan",
                "toolhive.component": component,
            },
        }
    return spec


def toolhive_mcpserver_audit() -> dict[str, object]:
    """``MCPServer.spec.audit`` — which accepts ONLY ``enabled``.

    ★ THE TWO CRDs ARE NOT SYMMETRIC HERE. ``MCPServer.spec.audit`` has exactly
    one property; the full option set (``includeRequestData``, ``eventTypes``,
    ``logFile``, …) exists only on ``VirtualMCPServer.spec.config.audit``. The
    MCPServer CRD is a structural schema with no
    ``x-kubernetes-preserve-unknown-fields``, so any extra key here is PRUNED by
    the API server. Verified against operations-qa on 2026-08-14: a merge patch
    carrying ``includeRequestData`` returns ``Warning: unknown field
    "spec.audit.includeRequestData"`` and stores ``{"enabled": true}``. The
    apply still succeeds, so an ``includeRequestData: false`` written here for
    safety would survive review as an assurance while being enforced by nothing.

    So the body-exclusion guarantee on this hop rests on ToolHive's own default
    (``IncludeRequestData`` defaults false, `pkg/audit/config.go`) rather than
    on anything declared here. See `toolhive_vmcp_audit` for the hop where it
    can be, and is, stated explicitly.

    Both hops log one JSON event per request — method, outcome, duration — to
    stdout, so it rides the existing pod-log path to Loki with no collector
    involved. That is why audit is on in every environment while OTLP is not.

    ── ``jsonrpc_error_message``: checked, and safe ONLY BY VERSION ──
    ``auditor.go`` writes ``jsonrpc_error_code``/``jsonrpc_error_message`` (256
    chars) into audit metadata whenever an outcome is ``ApplicationError``,
    gated ONLY on ``detectApplicationErrors`` (default true) and NOT on
    ``includeResponseData`` — its own comment says it reports them "without
    enabling full response data capture". This CRD exposes no switch for it, so
    if it fired here it could not be turned off.

    It does not fire for anything content-bearing, because it requires a
    TOP-LEVEL JSON-RPC ``error`` and ``pkg/mcp/response.go`` "intentionally
    omit[s] `result`". FastMCP 4.0.0b2 routes every tool-level failure into an
    ``isError`` result inside ``result``. Verified 2026-08-14 against a live
    streamable-http server: an exception echoing the caller's payload, a
    pydantic error carrying ``input_value=``, and an unknown tool ALL came back
    as ``isError`` results; only ``no/such/method`` produced a top-level error,
    message ``"Method not found"``. The detector also runs only on 2xx, so 401s
    and 5xx never reach it. What this deployment has actually produced at
    protocol level is ``-32603`` under load, message "Server returned an error
    response" — operational, no user content.

    ★ RE-CHECK THIS ON ANY FastMCP OR ToolHive UPGRADE. Nothing tests it, and
    both halves are load-bearing: if FastMCP started surfacing tool failures as
    protocol errors, or ToolHive started parsing ``result``, this would begin
    copying PRE-REDACTION request content — witan redacts inside the tool, after
    arguments arrive — into Loki, which is readable by anyone with Grafana
    access rather than being Cedar actor-scoped like the graph.
    ``mask_error_details`` is FastMCP's second line of defence here and it
    defaults to False; witan does not set it.

    Kept ON deliberately rather than tolerated: a JSON-RPC error inside an HTTP
    200 is exactly the ``-32603``-under-load signature this project is chasing,
    and disabling detection would record those as successes.
    """
    return {"enabled": True}


def toolhive_vmcp_audit() -> dict[str, object]:
    """``VirtualMCPServer.spec.config.audit`` — the full-schema counterpart.

    ★ `includeRequestData`/`includeResponseData` STAY FALSE. They are already
    the CRD's defaults; they are restated because witan's request bodies are
    the memories and tasks themselves, and turning either on would copy
    user-authored graph content into the log pipeline, where the redaction that
    governs the write path does not apply.

    ``detectApplicationErrors`` is deliberately left at its default (true) even
    though this CRD — unlike ``MCPServer.spec.audit`` — could disable it. See
    `toolhive_mcpserver_audit` for the evidence that it carries no user content
    at these versions, and for why the signal is wanted.
    """
    return {
        "enabled": True,
        "includeRequestData": False,
        "includeResponseData": False,
    }
