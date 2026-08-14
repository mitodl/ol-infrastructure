"""Shared ToolHive telemetry + audit configuration for every MCP stack.

A ToolHive deployment is at least two hops of Go binary in front of whatever
actually serves the tools: a ``VirtualMCPServer`` aggregator and, per backend,
an ``MCPServer`` proxyrunner. Both run their own OTel pipeline, configured
through an ``MCPTelemetryConfig`` CR rather than through the ``OTEL_*``
environment a Python or Go workload would read. Neither is on by default.

This module builds those specs. It lives in ``lib`` rather than under one
application because the witan and toolhive_swe stacks both need it and the
security-relevant decisions below — which hop may expose ``/metrics``, what an
audit event is allowed to carry — must not be re-derived per stack.

── Why CI gets logs but no traces or metrics ──
``setup_grafana`` (``substructure/aws/eks/grafana.py``) returns early for CI, so
operations-ci runs no Grafana Alloy: its ``grafana`` namespace exists and is
empty, and ``grafana-k8s-monitoring-alloy-receiver`` resolves in QA and
Production only. ``ships_telemetry`` mirrors that condition, so CI lands on the
no-exporter path by construction instead of buying a connection failure per
batch forever in the one environment nobody is watching.

Audit logging and the Prometheus ``/metrics`` path are NOT gated that way — see
their own notes below. Both work in CI.
"""

from ol_infrastructure.lib.pulumi_helper import StackInfo

# The in-cluster OTLP/HTTP receiver, shared with mit_learn, learn_ai and edxapp
# (see their Pulumi.{QA,Production}.yaml and edxapp/k8s_configmaps.py). Port
# 4318 is http/protobuf; 4317 on the same Service is gRPC, which we do not use.
# ToolHive's exporter is `otlptracehttp`/`otlpmetrichttp` (toolhive-core
# `telemetry/providers/otlp`), so 4318 is the correct one for it too.
OTLP_ENDPOINT = (
    "http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318"
)

# Matched to the mit_learn/learn_ai precedent rather than chosen fresh, so a
# trace crossing from one of those services is sampled consistently instead of
# being decided twice.
DEFAULT_TRACE_SAMPLING_RATE = "0.25"

# QA samples everything: it is where the concurrency probe runs, and a
# sampled-away trace is a probe run that measured nothing. Affordable precisely
# because QA carries no organic traffic.
_FULL_SAMPLE_ENVS = frozenset({"qa"})


def ships_telemetry(stack_info: StackInfo) -> bool:
    """Whether this environment has an OTLP receiver to export to.

    Mirrors ``setup_grafana``'s CI early-return. Kept as a named predicate so
    the reason a stack is dark is one grep away from the reason the collector
    is absent.

    The ``.lower()`` is redundant and deliberate: ``parse_stack`` builds
    ``env_suffix`` as ``stack_name.lower()``, so it is lowercase by
    construction. It is kept only so this predicate is character-for-character
    the condition ``setup_grafana`` tests — the two drifting apart is the
    failure this function exists to prevent.
    """
    return stack_info.env_suffix.lower() != "ci"


def telemetry_config_name(service: str, hop: str) -> str:
    """Name of the ``MCPTelemetryConfig`` CR for one hop of one service.

    ``hop`` is ``"backend"`` or ``"vmcp"``. There is one CR per hop rather than
    one shared between them because they differ on ``prometheus.enabled``, and
    that difference is a security boundary — see ``toolhive_telemetry_spec``.

    Namespace-scoped by necessity, not choice: the CRD refuses cross-namespace
    references "for security and isolation reasons", so every namespace running
    ToolHive needs its own pair.
    """
    return f"{service}-telemetry-{hop}"


def toolhive_service_name(stack_info: StackInfo, service: str, component: str) -> str:
    """Per-hop OTel service name, ``<env>-<service>-<component>``.

    ToolHive's CRD requires this to be unique per server, and it is the only
    thing separating an aggregator's spans from a proxy's — or one backend's
    from another's when several share a telemetry config. It is supplied
    through ``telemetryConfigRef.serviceName``, which overrides the config, so
    N backends can share one CR and still be told apart.

    Same ``<env>-<service>`` convention the OTEL_SERVICE_NAME env uses, so
    every hop of a stack sorts together in Grafana.
    """
    return f"{stack_info.env_suffix}-{service}-{component}"


def toolhive_trace_sampling_rate(stack_info: StackInfo) -> str:
    """Head sampling rate for the ToolHive hops.

    ★ THIS IS THE ROOT SAMPLING DECISION FOR THE WHOLE REQUEST. ToolHive
    receives the request first, so it starts the trace; a downstream workload
    running ``parentbased_traceidratio`` honours that decision rather than
    re-rolling it. So this value — not the workload's own sampler argument —
    sets what fraction of end-to-end traces exist.

    Production keeps the rate the other services use. Raising it there would
    change span cost against real traffic, which is a billing decision and not
    this module's to make.
    """
    if stack_info.env_suffix.lower() in _FULL_SAMPLE_ENVS:
        return "1.0"
    return DEFAULT_TRACE_SAMPLING_RATE


def toolhive_telemetry_spec(
    stack_info: StackInfo,
    service: str,
    component: str,
    *,
    expose_prometheus: bool,
) -> dict[str, object] | None:
    """Build an ``MCPTelemetryConfigSpec``, or ``None`` if nothing would be enabled.

    Returns ``None`` rather than an all-disabled spec so CI does not carry a CR
    that configures nothing — a referenced-but-inert config is the kind of thing
    that reads as "telemetry is on" to the next person to look.

    :param service: the stack, e.g. ``"witan"``. Becomes ``service.namespace``.
    :param component: the hop, e.g. ``"vmcp"`` or a backend's name. Recorded as
        ``toolhive.component``; the per-ref ``serviceName`` carries it too.
    :param expose_prometheus: whether to serve the Prometheus ``/metrics`` path.

        ★ MUST BE FALSE FOR ANY PUBLICLY-ROUTED HOP, WHICH MEANS EVERY vMCP WE
        RUN. ToolHive serves ``/metrics`` on the *main transport port* — there
        is no separate admin listener (toolhive `pkg/telemetry/config.go`: "The
        metrics are served on the main transport port at /metrics"). A vMCP's
        4483 is the port APISIX publishes, and both witan's and toolhive_swe's
        ``ingress.py`` route ``paths=["/*"]`` at it, so enabling the path there
        would put an unauthenticated ``https://<vmcp-host>/metrics`` on the
        public internet listing every tool name and its call counts. A backend
        proxy's 8080 is ClusterIP-only and never routed, so it is safe there.

        Revisit only alongside a path-level deny in the APISIX route — not by
        flipping this flag.

    The Prometheus path is enabled in EVERY environment including CI, unlike
    OTLP. It costs one HTTP route on a port already listening and opens no
    outbound connection, so ``ships_telemetry``'s reasoning does not apply to
    it. It is also the only read available in CI: port-forward, scrape before
    and after, and diff the cumulative histograms — a window bounded on BOTH
    sides by construction, which ``kubectl logs --since`` (which has no
    ``--until``) cannot give you.
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
            # passed whole so this and OTEL_EXPORTER_OTLP_ENDPOINT are visibly
            # the same endpoint. `insecure` is what actually selects plaintext,
            # and must stay in step with OTLP_ENDPOINT's scheme.
            "endpoint": OTLP_ENDPOINT,
            "insecure": True,
            "metrics": {"enabled": True},
            "tracing": {
                "enabled": True,
                "samplingRate": toolhive_trace_sampling_rate(stack_info),
            },
            # service.name is NOT set here — it comes from the per-hop
            # `telemetryConfigRef.serviceName` override, which is what lets one
            # spec serve several differently-named hops.
            "resourceAttributes": {
                "deployment.environment": stack_info.env_suffix,
                "service.namespace": service,
                "toolhive.component": component,
            },
        }
    return spec


#########################################
#   Audit                                #
#########################################
# Audit writes one JSON event per request — method, outcome, duration — to
# stdout (the CRD's default when `logFile` is unset), so it rides the existing
# pod-log path to Loki with no collector involved. That is why audit is on in
# every environment while OTLP is not.
#
# Event targets carry only the URL path, the MCP method and the resource id
# (the tool name) — `auditor.go:extractTarget` — never arguments. The one field
# that can carry payload-derived text is `jsonrpc_error_message`; see below.


def toolhive_mcpserver_audit() -> dict[str, object]:
    """``MCPServer.spec.audit`` — which accepts ONLY ``enabled``.

    ★ THE TWO CRDs ARE NOT SYMMETRIC. ``MCPServer.spec.audit`` has exactly one
    property; the full option set (``includeRequestData``, ``eventTypes``,
    ``detectApplicationErrors``, …) exists only on
    ``VirtualMCPServer.spec.config.audit``. The MCPServer CRD is a structural
    schema with no ``x-kubernetes-preserve-unknown-fields``, so any extra key
    here is PRUNED. Verified against operations-qa on 2026-08-14: a merge patch
    carrying ``includeRequestData`` returns ``Warning: unknown field`` and
    stores ``{"enabled": true}``. The apply still succeeds, so a
    ``includeRequestData: false`` written here for safety would survive review
    as an assurance while being enforced by nothing.

    Body exclusion on this hop therefore rests on ToolHive's own defaults
    (``IncludeRequestData``/``IncludeResponseData`` default false,
    `pkg/audit/config.go`), not on anything declared here.

    ── ``jsonrpc_error_message``: WHAT IT CAPTURES DEPENDS ON THE BACKEND'S SDK ──
    ``auditor.go:333-343`` copies ``jsonrpc_error_code``/``jsonrpc_error_message``
    (256 chars) into audit metadata whenever an outcome is
    ``ApplicationError``, gated ONLY on ``detectApplicationErrors`` (default
    true) and NOT on ``includeResponseData`` — its own comment says it reports
    them "without enabling full response data capture". This CRD exposes no
    switch for it, so on a backend it cannot be turned off.

    Capture requires a TOP-LEVEL JSON-RPC ``error``; ``pkg/mcp/response.go``
    "intentionally omit[s] `result`". Whether a tool failure lands in one or the
    other is decided by the BACKEND'S MCP SDK, not by ToolHive — so it has to be
    established per backend, not once. All six we run, verified 2026-08-14:

    * **FastMCP 4.0.0b2** (witan) — every tool-level failure becomes an
      ``isError`` result. Confirmed against a live server: an exception echoing
      the caller's payload, a pydantic error carrying ``input_value=``, and an
      unknown tool ALL stayed in ``result``; only ``no/such/method`` produced a
      top-level error, message ``"Method not found"``. Nothing content-bearing
      is captured.
    * **FastMCP** (toolhive_swe ``aws``) — mcp-proxy-for-aws 1.6.4 is FastMCP
      too, and its ``ToolErrorMiddleware.on_call_tool`` catches EVERY exception
      and re-raises ``ToolError``, which is the same path. This matters more
      than the others because the managed AWS endpoint behind it exposes a
      ``run_script`` tool, so its requests carry user-authored scripts.
    * **modelcontextprotocol/go-sdk** (``fetch``) — same outcome by a different
      route: ``mcp/server.go`` returns a structured ``*jsonrpc.Error`` directly
      but wraps a *plain* error in ``CallToolResult{IsError: true}``, and
      gofetch returns ``fmt.Errorf``. So its URL-bearing messages
      ("access to %s is disallowed by robots.txt") are NOT captured.
    * **@modelcontextprotocol/sdk** (``sentry``) — sentry-mcp returns
      ``{content, isError: true}`` from a handler-level catch, carrying a
      deliberate comment: "DO NOT change this to throw error - it breaks error
      handling!". Not captured.
    * **@modelcontextprotocol/sdk** (``context7``) — tool handlers return
      content results rather than throwing. Its one JSON-RPC error is a
      transport-level ``-32603`` emitted with **HTTP 500**, and the detector
      runs only on 2xx, so it is unreachable twice over; the message is the
      fixed string "Internal server error" regardless.
    * **mark3labs/mcp-go v0.55.0** (``grafana``) — ★ THE ONE THAT DIFFERS. A
      non-nil handler error becomes ``requestError{code: INTERNAL_ERROR}``,
      i.e. a top-level JSON-RPC error, so its messages ARE captured. Reviewed:
      mcp-grafana v1.0.0 interpolates queries, resource ids and upstream API
      status into those, but no credential — the service-account token travels
      as a header and appears in no error format string. Judged acceptable:
      operational text, 256-char cap, and it lands in the same Grafana Cloud
      the queries already target.

    The detector also runs only on 2xx, so 401s and 5xx never reach it.

    ★ A NEW BACKEND IS NOT COVERED BY THIS ANALYSIS. Adding one means checking
    its SDK's handler-error path before enabling audit on it — or leaving
    ``audit`` off for that backend until someone has.

    ★ RE-CHECK ON ANY SDK OR ToolHive UPGRADE. Nothing tests this, and it is
    the only thing standing between an audit log and payload-derived content.
    For a backend serving user-authored data the cost would be real: content
    reaches Loki, which is readable by anyone with Grafana access rather than
    being scoped the way the data's own store is, and it would arrive
    PRE-redaction where a stack redacts inside the tool.

    Kept ON deliberately rather than tolerated: a JSON-RPC error inside an HTTP
    200 is exactly the ``-32603``-under-load signature the witan concurrency
    work is chasing, and disabling detection would record those as successes.
    """
    return {"enabled": True}


def toolhive_vmcp_audit() -> dict[str, object]:
    """``VirtualMCPServer.spec.config.audit`` — the full-schema counterpart.

    ★ ``includeRequestData``/``includeResponseData`` STAY FALSE. They are
    already the CRD's defaults; they are restated because a vMCP relays whole
    request bodies, and turning either on would copy them into the log
    pipeline, where whatever redaction governs the backend's own write path
    does not apply.

    ``detectApplicationErrors`` is deliberately left at its default (true) even
    though this CRD — unlike ``MCPServer.spec.audit`` — could disable it. See
    ``toolhive_mcpserver_audit`` for the per-SDK evidence on what that field
    can carry, and for why the signal is wanted.
    """
    return {
        "enabled": True,
        "includeRequestData": False,
        "includeResponseData": False,
    }
