"""witan's serving tier: a plain Deployment and Service, no ToolHive.

This module replaces ``mcp_servers.py`` (the ``MCPGroup`` + ``MCPServer``) and
the ``VirtualMCPServer`` that used to sit in ``__main__.py``. APISIX now routes
straight to witan's own FastMCP process.

── WHY TOOLHIVE IS GONE ──

Not because ToolHive is bad — ``toolhive_swe`` keeps it, and should. Because
what it provides is redundant *for witan specifically*, while what it costs is
the defect that blocked this project:

  * **Its OIDC was a second lock on the same door.** witan validates the
    Keycloak JWT itself (``server.py``'s ``JWTVerifier``, agent-kit ADR-0004 D1)
    and derives the per-request actor from ``sub``. The vMCP's ``incomingAuth``
    validated the same token against the same issuer moments earlier, then had
    to be told to forward it (``passthroughHeaders``) — plus a header-injection
    ``MCPExternalAuthConfig`` that authenticated nothing and existed only to
    stop a probe 401 being read as misconfiguration. Three moving parts to end
    up where witan already was.
  * **It aggregated a group of one.** ``priorityOrder: ["witan"]``, and the
    code-graph tools are mounted IN-PROCESS by ``witan serve``, not run as a
    second workload. ``authzConfig`` was null; ``authServerConfig`` absent.
  * **Its telemetry measured hops that no longer exist.**

Against that:

  * **A hardcoded 30s deadline on ``tools/call``**, in three separate upstream
    constants, with the CRD field that looks like the knob
    (``spec.config.operational.timeouts``) read by nothing at runtime. This is
    the one that mattered. Measured in QA on 2026-08-15 at 16 concurrent
    writers: the store committed **16 of 16** writes, and 15 callers were told
    their write failed. Per-hop trace ``003cfcd836f5f9963dd3cd6c7722421b`` —
    proxy receive to witan handler start 2459.63ms, ToolHive cut at
    29997.06ms, witan committed at 33601.53ms, 6064.10ms after nobody was
    listening. Not a slow write: an **indeterminate** one.
  * **~289ms per call of pure tier overhead when idle** (65.90 vMCP inbound +
    102.19 hop + 111.58 pre-handler + 8.97 return), rising to ~2.46s
    pre-handler at 16 writers — 8.2% of the very budget it was enforcing,
    spent before witan saw the request.
  * **A vMCP memory ceiling around 32 concurrent sessions** (~15Mi each), which
    OOMKilled the aggregator and took the whole endpoint down with it.
  * **An upgrade freeze.** ToolHive 0.43 rejects ``Authorization`` in
    ``passthroughHeaders`` at startup (upstream #6235) — the exact setting
    witan's per-actor auth depended on — so the stack was pinned at 0.42.1 and
    0.43 offered nothing on the deadline in exchange.

The deadline does not disappear; it becomes **ours**. See
``WITAN_REQUEST_TIMEOUT`` below.

── WHAT WAS LOST, AND WHAT REPLACED IT ──

* Health probes lived on the operator's proxy container, so witan had none of
  its own. It now serves ``GET /health`` (agent-kit witan-council 0.13.0),
  deliberately shallow — see ``WITAN_HEALTH_PATH``.
* The ``witan-sa`` ServiceAccount was operator-created and owner-referenced by
  the ``MCPServer``, so it is deleted along with it. This stack creates its own
  as ``witan-server`` — NOT ``witan-sa``, which cannot be reused during the
  cutover; see ``WITAN_SERVICE_NAME``. It carries no IRSA annotations, as the
  operator's did not either (verified against the live SA), because witan
  reaches only omnigraph-server and Keycloak — and for the same reason its
  token is not projected into the pod at all.
* Per-request audit JSON came from the ToolHive tier. witan's own structured
  logging (``witan_log_env``) already emits a line per tool call to the same
  Loki pipeline, and its OTel spans are unaffected — those come from
  ``witan_core.observability``, inside the process.
* ``spec.resources`` being silently dropped by the operator (which ran the
  backend BestEffort until it was restated through a ``podTemplateSpec``
  patch) is simply gone as a failure mode: a Deployment applies the resources
  it declares.
"""

from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.applications.witan.observability import (
    downward_api_env_args,
    otel_env,
    sentry_env,
    witan_log_env,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# The Deployment, Service and ServiceAccount all share this name. The Service
# is what APISIX routes to (see ``ingress.py``); the kubelet's probes reach the
# pod's own IP directly and do not go through it.
#
# ★ `witan-server`, NOT `witan`, AND THE MIGRATION IS WHY. The ToolHive operator
# already owns a proxy-runner Deployment named exactly `witan` in this namespace
# (plus a `witan` StatefulSet — only the kind told them apart), and it owns the
# `witan-sa` ServiceAccount by ownerReference. Pulumi performs deletions LAST,
# so a new Deployment claiming either name is created before the resources
# holding it are removed, and fails:
#
#     creation failed: deployments.apps "witan" already exists
#     creation failed: serviceaccounts "witan-sa" already exists
#
# Caught by `pulumi preview` against the live CI stack rather than discovered
# during the cutover. Both old objects are ownerReference'd by the MCPServer, so
# they are garbage-collected when it goes — but not before these are created.
#
# `-server` also matches the house style of the tier this talks to
# (`omnigraph-server`, applications/omnigraph/data_tier.py) and disambiguates
# the workload from the namespace and Pulumi project, which are both `witan`.
WITAN_SERVICE_NAME = "witan-server"
WITAN_SERVICE_ACCOUNT_NAME = "witan-server"

# witan's FastMCP listener. Same port the MCPServer's `mcpPort` named, so the
# container's command line is unchanged; what goes away is the proxy that used
# to sit in front of it on 8080.
WITAN_PORT = 8000

# The MCP endpoint path. Equal to witan's own default, and PASSED EXPLICITLY as
# `--path` below rather than relied upon, because it is half of the
# client-facing URL — `https://witan.<env>.ol.mit.edu/mcp` — and that URL is
# unchanged by this migration only because witan serves the same path the vMCP
# did.
#
# Declaring it is the point: left implicit, the public URL of this service would
# be a default inside an agent-kit release, and a change to it there would break
# every configured client at once with nothing in this repo to review. Pinned
# here, such a change is inert until someone edits this line.
WITAN_MCP_PATH = "/mcp"

# Liveness/readiness. Shallow by design on witan's side: it answers from
# process state and never touches the graph.
#
# ★ THAT SHALLOWNESS IS THE WHOLE POINT, and this stack has the incident to
# prove it. ToolHive's proxy `/health` synchronously pinged its MCP backend
# (upstream `pkg/healthcheck/healthcheck.go:CheckHealth`). On 2026-08-12 a
# burst of 16 concurrent writes saturated that backend, the ping stopped
# answering, and the kubelet's 5s liveness probe killed a container that was
# working perfectly — which removed the only endpoint APISIX had and turned a
# 30s write queue into ~60s of outage for readers too. A deep probe converts
# backend SLOWNESS into frontend DEATH, and fires exactly when killing the pod
# is most harmful.
#
# So: do not "improve" these probes by pointing them at something that talks to
# omnigraph. A graph outage is real and belongs in alerting on the spans witan
# already emits, where it degrades a dashboard instead of a pod.
WITAN_HEALTH_PATH = "/health"

# ── Startup budget ───────────────────────────────────────────────────────────
# Measured in QA on 2026-08-15 from the running pod: container start 22:25:17,
# `Uvicorn running on http://0.0.0.0:8000` at 22:25:21.9 — ~4.9s to serving.
#
# The startupProbe gates the other two: while it runs the kubelet suppresses
# liveness and readiness entirely, and once it passes it never runs again.
#
# ★ THE BUDGET IS A RANGE, NOT A NUMBER, because a probe worker does not overlap
# attempts — the next one starts after the previous returns, so the effective
# interval is roughly max(period, how long the attempt took).
#
#   * FAILING FAST (the normal boot): the port is not bound yet, so each attempt
#     is an immediate connection-refused and the interval is the 3s period. The
#     Nth failure lands at initial_delay + (N-1) x period → killed at
#     5 + 19x3 = ~62s, ~12x the measured 4.9s boot.
#   * FAILING SLOW (the port binds but the handler never answers): each attempt
#     burns the full 5s timeout below, so the same 20 failures stretch to
#     roughly 5 + 20x5 = ~105s.
#
# An earlier version of this comment stated only the 62s figure, which was true
# when `timeoutSeconds` was the inherited 1s and is not once the timeout exceeds
# the period. Raised by review on PR #5449.
#
# ★ THE THRESHOLD STAYS AT 20 RATHER THAN DROPPING TO ~12 TO RESTORE 62s. The
# headroom exists for a specific uncontrolled dependency: fastmcp performs an
# outbound version check against pypi.org during startup (visible in the pod
# logs as `GET https://pypi.org/pypi/fastmcp/json`). Tightening to 12 would cut
# the FAST-path budget — the one that actually governs real boots — from 62s to
# ~38s, or under 8x the measured boot, to buy a shorter kill on a
# bound-but-wedged process that has never been observed. Slower detection of a
# hypothetical beats killing a slow-but-healthy start.
#
# Either way a boot that blows this is stuck, not slow, and killing it is right.
WITAN_STARTUP_INITIAL_DELAY_SECONDS = 5
WITAN_STARTUP_PERIOD_SECONDS = 3
WITAN_STARTUP_FAILURE_THRESHOLD = 20

# ── ★ HOW LONG A PROBE MAY WAIT, WHICH IS THE VALUE THAT CAUSED AN OUTAGE ─────
#
# These were never set, so all three inherited Kubernetes' default of ONE
# SECOND, and that single unwritten number produced the failure below.
#
# MEASURED IN QA, 2026-08-16, first concurrency probe after the ToolHive
# removal. 16 concurrent writers:
#
#   * witan answered EVERY call correctly — 16/16 `memory_store` `outcome: ok`,
#     all 236 HTTP responses 200, slowest 27.1s against the 120s budget below;
#   * but `GET /health` could not be answered inside 1s under that load;
#   * readiness failed 3x at periodSeconds 5, so the pod went NotReady
#     (`Ready=True last=19:27:40` against a write window of 19:27:00-19:27:37);
#   * Kubernetes removed it from the Service, and APISIX had nothing to route
#     to — `failed to set upstream: no valid upstream node`, HTTP 503,
#     `upstream_addr=-`, `request_time=0.001`, never contacting witan;
#   * so 15 of 16 callers were told their write failed while every row committed.
#     An INDETERMINATE write, produced entirely by probe configuration.
#
# ★ SHALLOWNESS DOES NOT SAVE YOU HERE, and the handler docstring above is why
# this comment exists. `/health` is deliberately shallow so it can never block
# on the graph — that defends against the HANDLER stalling. It does nothing
# about the EVENT LOOP being starved: a shallow coroutine still has to be
# SCHEDULED, and under enough concurrency on one process it is not scheduled
# within a second. Same "backend slowness becomes frontend death" failure, one
# layer down.
#
# ★ AND AT ONE REPLICA, READINESS CAN ONLY EVER CAUSE A TOTAL OUTAGE. Readiness
# exists to pull a sick pod out of a POOL; witan is pinned to a single replica
# on purpose (see `replicas=1`), so there is no degraded mode to fall back to —
# ejection is the whole service, and it fires hardest exactly when load is
# highest. That asymmetry is why readiness is the most forgiving of the three
# below rather than the strictest.
#
# Liveness is the most patient of all: killing a saturated-but-working process
# throws away in-flight writes, and restarting it does not make the graph
# faster. 10s x 3 failures x 20s period is ~60s of sustained unresponsiveness
# before a restart, which a genuinely wedged process will still reach.
#
# These do NOT address WHY /health cannot answer in 1s under load — that is
# unresolved and tracked separately (CPU throttling read 0 and usage 0.008
# cores, but Prometheus sampled far too coarsely to resolve a 37-second event).
# They make saturation degrade instead of amputating the service, which is the
# same shape of fix as the ToolHive proxy's ping timeout before it.
WITAN_READINESS_TIMEOUT_SECONDS = 5
WITAN_READINESS_PERIOD_SECONDS = 5
WITAN_READINESS_FAILURE_THRESHOLD = 6
WITAN_LIVENESS_TIMEOUT_SECONDS = 10
WITAN_STARTUP_TIMEOUT_SECONDS = 5

# ── The request deadline, which is now ours ──────────────────────────────────
# ★ REMOVING TOOLHIVE DOES NOT REMOVE THE DEADLINE — it moves it here, and the
# difference is that this number is chosen and tunable where ToolHive's 30s was
# hardcoded in three places.
#
# Left unset, the deadline would silently become APISIX's upstream default,
# which is inherited rather than declared and is not visible from this stack.
# `BackendTrafficPolicy` (apisix.apache.org/v1alpha1, present on the cluster
# and the supported lever for Gateway API routes) makes it explicit.
#
# 120s against a measured worst case of 33.6s at 16 concurrent writers, and
# 3.45s solo. The point is NOT to make writes fast — it is that a write which
# needs 34s now returns a result instead of a 502 whose outcome nobody can
# determine. Slow-but-correct beats fast-and-ambiguous for a memory graph,
# because an indeterminate write is the one failure a caller cannot safely
# retry.
#
# Raising this further has a real cost: a wedged call holds a connection for
# the full budget. Lowering it back toward 30s reintroduces exactly the defect
# this change exists to remove.
#
# `connect` is generous for an in-cluster ClusterIP hop that should resolve
# instantly; if connect is the thing timing out, the pod is gone and 10s of
# patience changes nothing but the error text.
WITAN_REQUEST_TIMEOUT_SECONDS = 120
WITAN_REQUEST_TIMEOUT = f"{WITAN_REQUEST_TIMEOUT_SECONDS}s"
WITAN_CONNECT_TIMEOUT = "10s"
WITAN_SEND_TIMEOUT = "60s"

# ── Shutdown budget, which MUST exceed the request budget ────────────────────
# ★ A GRACE PERIOD SHORTER THAN THE REQUEST BUDGET RECREATES THE EXACT DEFECT
# THIS PR REMOVES, just with a different executioner. Kubernetes defaults to 30s:
# it sends SIGTERM, waits, then SIGKILLs. A write measured at 33.6s under load
# would be killed mid-commit during a rollout, an eviction, or a node
# drain — the caller gets a severed connection and cannot tell whether the write
# landed, which is the definition of the indeterminate write. Trading ToolHive's
# 30s cut for the kubelet's 30s cut would have been no trade at all.
#
# Derived from the request budget rather than written as its own literal,
# because the invariant is `grace > budget` and two independent numbers drift.
# Raising WITAN_REQUEST_TIMEOUT_SECONDS now moves this with it.
#
# The margin covers uvicorn noticing SIGTERM and finishing a call already at the
# far end of the budget; it does not need to be large, only positive. The cost is
# that a node drain can wait this long for witan — acceptable for a
# single-replica workload whose deploys are already a brief outage by design.
#
# ★ THIS IS ONLY HALF THE MECHANISM, AND THE OTHER HALF LIVES IN agent-kit.
# An earlier version of this comment asserted that uvicorn "waits for in-flight
# requests rather than dropping them", so a long grace period was sufficient.
# It is not: FastMCP constructs its uvicorn config with a hardcoded
# `timeout_graceful_shutdown: 2` (fastmcp `server.py`, `run_http_async`), so on
# SIGTERM uvicorn gives in-flight work TWO SECONDS and then drops it — a 27s
# write is severed regardless of how patient the kubelet is being.
#
# So this number is necessary and not sufficient. witan must also pass
# `uvicorn_config={"timeout_graceful_shutdown": ...}` through `mcp.run()`;
# tracked in agent-kit. Until it does, the grace period below buys the pod time
# that uvicorn declines to use.
# ── preStop drain, which closes the LAST no-endpoint gap ─────────────────────
# ★ maxUnavailable=0 removed the window where NO pod was Ready. It did not
# remove the window where APISIX still routes to a pod that has already stopped
# accepting, and that window produced a real failed read.
#
# MEASURED, not reasoned. Rolling witan-server mid-storm against CI on
# 2026-08-20 left one of eight readers degraded. The client reported a bare
# JSON-RPC -32603, which invited the wrong explanation (per-pod MCP session
# state). It was not: the deployed connection is stateless (witan ADR-0006),
# there was no `task_get` error in either pod's log, and Sentry captured
# nothing. The APISIX access log had the answer — a single
#     host=witan.ci.ol.mit.edu status=502 upstream_status=502
#     upstream_connect_time=0.000 upstream_header_time=-
# at 12:36:48, the same second the old pod logged "Waiting for connections to
# close". APISIX connected to the terminating pod and never got a response
# header. The MCP client surfaces that gateway 502 as -32603
# (`mcp/client/streamable_http.py`: any non-2xx that is not a 404 becomes
# INTERNAL_ERROR / "Server returned an error response"), which is why it looked
# like a server-side internal error and left no server-side trace.
#
# ★ IT IS RARE, NOT PER-ROLLOUT. One occurrence in six RollingUpdate rollouts
# observed so far: it appeared in the first one, and a subsequent 5-run sweep
# under identical load put 1,735 requests through APISIX with zero non-200s.
# That is a reason to state the rate honestly, not a reason to leave it: the
# failure is one unlucky request being told a write or read failed when the
# deployment was healthy, which is the indeterminate outcome this stack exists
# to remove. The hook removes the window rather than lowering its odds, and a
# rare defect that only appears during a deploy is exactly the kind that gets
# misattributed for months.
#
# WHY IT HAPPENS. Pod deletion fans out to two consumers that do not
# synchronise: the kubelet (SIGTERM) and the endpoints controller (EndpointSlice
# removal, then the APISIX ingress controller, then the APISIX data plane).
# uvicorn stops accepting the instant SIGTERM lands, so every request APISIX
# routes during that propagation delay hits a socket nobody is serving.
#
# A preStop hook is what decouples them: the kubelet runs it BEFORE SIGTERM, so
# the pod keeps serving normally for the whole propagation window and is
# signalled only once APISIX has stopped sending it traffic. Ten seconds is
# generous for a watch-driven path measured in low single-digit seconds; the
# cost is ten seconds added to every rollout of a workload whose deploys are
# already minutes long.
#
# Native `sleep` action rather than `exec: sleep 10` on purpose: the container
# runs a distroless-style image and `readOnlyRootFilesystem: true`, so relying
# on a shell binary is a dependency on the base image that this does not need.
# Requires Kubernetes >= 1.30 (GA); the clusters run 1.36.
WITAN_PRESTOP_DRAIN_SECONDS = 10

# ── Shutdown budget, which MUST exceed the preStop drain + the request budget ─
# The drain is added rather than absorbed. The kubelet's grace period is the
# budget for EVERYTHING after the delete: preStop first, and only then SIGTERM
# and uvicorn's own graceful shutdown (120s — agent-kit's
# DEFAULT_SHUTDOWN_GRACE_SECONDS, deliberately matched to the request budget).
#
# Absorbing the 10s into the existing 150s would NOT have shortened a write:
# uvicorn would still get its full 120s, since 150 - 10 = 140 > 120. What it
# would have eaten is the MARGIN, from 30s down to 20s — the slack that covers
# uvicorn noticing SIGTERM and finishing a call already at the far end of the
# budget. Adding instead of absorbing keeps that margin at the value the ladder
# was reasoned about with, and keeps the invariant readable as
# `grace > drain + budget` rather than as arithmetic a later reader has to redo.
WITAN_TERMINATION_GRACE_SECONDS = (
    WITAN_PRESTOP_DRAIN_SECONDS + WITAN_REQUEST_TIMEOUT_SECONDS + 30
)

# Mount path (inside the container) for the actor-tokens Secret volume.
ACTOR_TOKENS_MOUNT_PATH = "/etc/witan/actor-tokens"  # pragma: allowlist secret
ACTOR_TOKENS_FILENAME = "tokens.json"  # pragma: allowlist secret

# Writable scratch space. The container runs `readOnlyRootFilesystem: true`
# with no writable volume, so Python's tempfile found nothing in
# ['/tmp', '/var/tmp', '/usr/tmp', '/src'] and every server-side tool that
# needs a temp file failed outright — verified against CI on 2026-08-07, where
# `witan migrate merge` through the MCP tier returned "No usable temporary
# directory found" at every payload size, including --dry-run. Those tools hand
# a file to the `omnigraph` binary, so there is no in-memory fallback.
#
# Disk-backed on purpose: `medium: Memory` would charge this to the pod's
# memory limit and OOM the server on a large export.
#
# Writability depends on `fsGroup`: kubelet group-owns an emptyDir by it, and
# the container runs as uid/gid 1000, so without it the mount is root-owned and
# useless — the same trap ci_indexer.py documents on its scratch volume. Under
# ToolHive this happened to work because the operator set `fsGroup: 1000` of its
# own accord; it is declared explicitly below now that nothing else will.
TMP_MOUNT_PATH = "/tmp"  # noqa: S108
TMP_FS_GROUP = 1000

# Sized for the server-side work, which is dominated by the graph export
# `store_merge` takes of its OWN target to reconcile against — that grows with
# the shared graph, not with the caller's upload (client batches are capped near
# 2 MiB by witan_core.chunking.MCP_LOAD_MAX_BYTES). 2Gi is well clear of a
# council graph's export today and far below the indexer's 8Gi.
TMP_SIZE_LIMIT = "2Gi"

# ★ THESE NOW ACTUALLY APPLY. Under ToolHive the identical values were declared
# on `MCPServer.spec.resources`, which the operator accepted and never passed to
# the container — the rendered StatefulSet showed `resources: {}` and the pod ran
# **BestEffort**, first in line for eviction under node memory pressure. The
# workaround was to restate them through a `podTemplateSpec` patch and keep the
# two in step (tk-toolhive-operator-drops-mcpserver-spec-resources-8ea1ff).
# A Deployment applies what it declares, so there is one declaration again.
# ★ NO CPU LIMIT, DELIBERATELY — only a request. A CPU limit is enforced by CFS
# quota: once the container's share is spent the kernel STOPS SCHEDULING IT for
# the rest of the 100ms period, even on an idle node. For a single-replica,
# latency-sensitive service that is the worst possible trade — it converts spare
# node capacity into stalls, and a stalled event loop is what could not answer
# `/health` inside a probe timeout on 2026-08-16 and got the only replica pulled
# out of the Service. The `requests` value is what actually matters: it is the
# guaranteed share and the scheduler's placement input. Above it, witan may now
# burst into whatever the node has spare.
#
# The MEMORY LIMIT STAYS. Memory is incompressible — there is no "throttle", only
# the OOM killer — so an unlimited container can take its node's neighbours down
# with it. CPU throttling degrades one pod; memory exhaustion degrades a node.
#
# ★ THE MEMORY REQUEST:LIMIT RATIO IS LOAD-BEARING. The VPA in `__main__.py`
# controls this container with `controlledValues: RequestsAndLimits`, scaling the
# limit to PRESERVE this ratio while bounding only the request. 256Mi:512Mi is
# 2:1, so its 1Gi `maxAllowed` caps the effective limit at 2Gi. Widening the
# ratio silently raises that ceiling.
#
# These values also NOW ACTUALLY APPLY. Under ToolHive the identical numbers sat
# on `MCPServer.spec.resources`, which the operator accepted and never passed to
# the container — the rendered StatefulSet showed `resources: {}` and the pod ran
# **BestEffort**, first in line for eviction under node memory pressure
# (tk-toolhive-operator-drops-mcpserver-spec-resources-8ea1ff). A Deployment
# applies what it declares.
# Defaults, overridable per stack via `witan:cpu_request` / `witan:memory_request`
# / `witan:memory_limit` — see `witan_resources` and the call site in
# `__main__.py`. Per-stack because the environments differ in corpus size and
# therefore in what a write costs, and because retuning under load should not
# need a code release.
DEFAULT_CPU_REQUEST = "250m"
DEFAULT_MEMORY_REQUEST = "256Mi"
DEFAULT_MEMORY_LIMIT = "512Mi"


def witan_resources(
    cpu_request: str = DEFAULT_CPU_REQUEST,
    memory_request: str = DEFAULT_MEMORY_REQUEST,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
) -> kubernetes.core.v1.ResourceRequirementsArgs:
    """Build the container's resources, with the CPU limit deliberately absent.

    ★ NO CPU LIMIT, ONLY A REQUEST. A CPU limit is enforced by CFS quota: once
    the container's share is spent the kernel STOPS SCHEDULING IT for the rest of
    the 100ms period, even on a completely idle node. For a single-replica,
    latency-sensitive service that is the worst trade available — it converts
    spare node capacity into stalls, and a stalled event loop is exactly what
    failed to answer `/health` inside a probe timeout on 2026-08-16 and got the
    only replica pulled out of the Service. `requests` is what actually matters:
    the guaranteed share, and the scheduler's placement input. Above it, witan
    may burst into whatever the node has spare.

    ★ THE MEMORY LIMIT STAYS, and the asymmetry is the point. Memory is
    incompressible — there is no throttle, only the OOM killer — so an unbounded
    container can take its node's neighbours with it. CPU throttling degrades one
    pod; memory exhaustion degrades a node.

    ★ THE MEMORY REQUEST:LIMIT RATIO IS LOAD-BEARING, which is why both are
    settable and neither should move alone. The VPA in ``__main__.py`` runs
    ``controlledValues: RequestsAndLimits``: it bounds the REQUEST by its
    ``maxAllowed`` and scales the limit to preserve this ratio. At the 2:1
    default, a 2Gi ``maxAllowed`` caps the effective limit at 4Gi. Set
    ``memory_limit`` to 4x the request and that ceiling silently becomes 8Gi.
    """
    return kubernetes.core.v1.ResourceRequirementsArgs(
        requests={"cpu": cpu_request, "memory": memory_request},
        limits={"memory": memory_limit},
    )


# The uid/gid the image runs as, and the hardening the ToolHive operator used to
# apply on witan's behalf. Reproduced verbatim from the live StatefulSet rather
# than reinvented, so dropping the operator does not quietly relax the pod's
# security posture — that would be a real regression hidden inside a migration.
WITAN_RUN_AS = 1000


class WitanServingTier(NamedTuple):
    """Handles for depends_on wiring and for the VPA's target."""

    deployment: kubernetes.apps.v1.Deployment
    service: kubernetes.core.v1.Service


def create_serving_tier(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_image: str | Output[str],
    omnigraph_server_addr: str | Output[str],
    council_graph_id: str | Output[str],
    oidc_issuer: str,
    oidc_audience: str,
    actor_tokens_secret_name: str,
    actor_tokens_secret: Resource,
    witan_ci_token_secret_name: str,
    witan_ci_token_secret_key: str,
    witan_ci_token_secret: Resource,
    witan_code_token_secret_name: str,
    witan_code_token_secret_key: str,
    witan_code_token_secret: Resource,
    sentry_dsn_secret_name: str,
    sentry_dsn_secret_key: str,
    sentry_dsn_secret: Resource,
    migration_job: Resource,
    service_version: str,
    remote_write_max_inflight: str = "",
    remote_write_queue_seconds: str = "",
    remote_call_budget_seconds: str = "",
    cpu_request: str = DEFAULT_CPU_REQUEST,
    memory_request: str = DEFAULT_MEMORY_REQUEST,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
) -> WitanServingTier:
    """Provision witan's ServiceAccount, Deployment, Service and route policy.

    ``remote_write_max_inflight`` / ``remote_write_queue_seconds`` retune
    witan's client-side write admission. Empty (the default) leaves witan's own
    defaults in force — the env var is omitted entirely rather than set to an
    empty string, so "unset" and "set to nothing" cannot diverge between what
    Pulumi declares and what witan reads.

    ``remote_call_budget_seconds`` tells witan how long a tool call has before
    something upstream stops waiting for it, so it can refuse a write it cannot
    finish rather than be cut off mid-call. witan-core assumes no deadline of
    its own — the same library runs from a CLI and from a batch Job — so this
    is the deployment declaring one. It must be kept in step with
    ``WITAN_REQUEST_TIMEOUT``; see the call site.
    """
    witan_service_account = kubernetes.core.v1.ServiceAccount(
        f"witan-service-account-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=WITAN_SERVICE_ACCOUNT_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
    )

    pod_labels = k8s_global_labels | {"app.kubernetes.io/name": WITAN_SERVICE_NAME}

    witan_deployment = kubernetes.apps.v1.Deployment(
        f"witan-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=WITAN_SERVICE_NAME,
            namespace=namespace,
            labels=pod_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            # ★ SINGLE REPLICA IS LOAD-BEARING, NOT A STARTING POINT.
            # witan's client-side write gate (`witan_core.omnigraph._WriteGate`)
            # is the GLOBAL bound on writes in flight against one graph, and it
            # is per-process. Two replicas means two gates, each admitting its
            # own quota while the data tier — which serialises writes on one
            # graph regardless — sees the sum. The cap would read as 4 and
            # behave as 8.
            #
            # Scaling this out is a real option, but it is a design change
            # (moving admission to the data tier or a shared lease), not a
            # replica-count edit. Do NOT add an HPA.
            replicas=1,
            # ── RollingUpdate, changed from Recreate on 2026-08-20 ───────────
            # Recreate was chosen to avoid ever running two pods, on the
            # grounds that two pods means two write gates. That reasoning is
            # still correct for STEADY-STATE replicas, which is why `replicas`
            # above stays at 1 and an HPA is still the wrong answer. It does
            # not hold for a rollout, and the cost of applying it there was
            # measured rather than assumed.
            #
            # WHAT RECREATE COST. Recreate tears the only pod down before the
            # new one starts, so every deploy has a window with ZERO endpoints.
            # Measured against CI on 2026-08-20 by restarting witan-server
            # during a 16-writer burst: 8 of 8 readers degraded, and 5 writes
            # committed server-side while their callers were told the write
            # failed. Six writes landed; one caller found out. That is the
            # indeterminate write this stack exists to remove, re-entering
            # through the rollout door.
            #
            # WHY TWO GATES DOES NOT ACTUALLY HAPPEN HERE. uvicorn stops
            # ACCEPTING on SIGTERM and only drains what it already holds,
            # so the old pod's gate admits nothing new from that moment. This
            # was confirmed in the same run, not reasoned from the docs: of the
            # 21 handlers that completed after SIGTERM, every one had started
            # before it (latest start 219ms ahead of the signal), and the drain
            # ran 19.6s while 15 in-flight writes finished. The genuinely
            # two-gates window is only from "new pod Ready" to "old pod
            # signalled", i.e. controller reaction time, not the length of the
            # drain.
            #
            # maxUnavailable=0 is the load-bearing half: it keeps a Ready
            # endpoint at all times, which is what removes the outage. maxSurge=1
            # is the minimum that allows that with a single replica.
            #
            # ★ THE IN-FLIGHT HALF IS NOW MEASURED, and it holds. The open
            # question above — whether a request already on the terminating pod
            # still returns once the endpoint leaves the EndpointSlice — was
            # answered by re-running `witan.scripts.concurrency_probe` across a
            # rollout, which is what that paragraph asked for. See the sweep
            # recorded on tk-a-witan-server-rollout-hands-writers-an-error-fo-62cd3d.
            # The whole 16-writer storm was served by the OLD pod during its
            # drain, client and server agreed exactly on which writes landed,
            # and the INDETERMINATE bucket stayed empty.
            #
            # What that sweep also found is that maxUnavailable=0 is not the
            # last gap: it guarantees a Ready endpoint, not that APISIX has
            # STOPPED USING the terminating one. See WITAN_PRESTOP_DRAIN_SECONDS
            # for the 502 that came through that gap and the hook that closes it.
            strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
                type="RollingUpdate",
                rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                    max_unavailable=0,
                    max_surge=1,
                ),
            ),
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app.kubernetes.io/name": WITAN_SERVICE_NAME}
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=pod_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=WITAN_SERVICE_ACCOUNT_NAME,
                    # witan reaches omnigraph-server and Keycloak, and never the
                    # Kubernetes API — so the default projected token would be a
                    # cluster credential sitting inside the one process in this
                    # namespace that is reachable from the internet. Same
                    # reasoning, and the same setting, as `ci_indexer.py`.
                    automount_service_account_token=False,
                    # Must outlast the request budget, or a rollout/drain kills
                    # a write mid-commit and hands the caller exactly the
                    # indeterminate outcome this stack exists to remove. See
                    # WITAN_TERMINATION_GRACE_SECONDS.
                    termination_grace_period_seconds=WITAN_TERMINATION_GRACE_SECONDS,
                    # What makes the /tmp emptyDir writable by a non-root
                    # container — see TMP_FS_GROUP. Under ToolHive this was the
                    # operator's default and worked by luck; here it is ours.
                    security_context=kubernetes.core.v1.PodSecurityContextArgs(
                        fs_group=TMP_FS_GROUP,
                        run_as_user=WITAN_RUN_AS,
                        run_as_group=WITAN_RUN_AS,
                        run_as_non_root=True,
                        seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                            type="RuntimeDefault",
                        ),
                    ),
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            # Still `mcp`, which is what the ToolHive operator
                            # called it. The WORKLOAD had to be renamed (see
                            # WITAN_SERVICE_NAME); the container did not, so it
                            # keeps the name the VPA's `container_name`, saved
                            # Grafana/Loki queries, and `kubectl logs -c` all
                            # already use.
                            name="mcp",
                            image=witan_image,
                            args=[
                                "serve",
                                "--transport",
                                "streamable-http",
                                "--host",
                                "0.0.0.0",  # noqa: S104
                                "--port",
                                str(WITAN_PORT),
                                "--path",
                                WITAN_MCP_PATH,
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    name="http",
                                    container_port=WITAN_PORT,
                                )
                            ],
                            env=[
                                # Pod identity for spans and log lines.
                                # `witan_core.observability` reads these ONCE at
                                # import, so they must be present at startup.
                                *downward_api_env_args(),
                                # Direct OIDC/JWT validation against Keycloak
                                # (agent-kit ADR-0004 D1). witan is the identity
                                # boundary — and with ToolHive gone it is the
                                # ONLY one, which is what it always effectively
                                # was.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_OIDC_ISSUER", value=oidc_issuer
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_OIDC_AUDIENCE", value=oidc_audience
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_ACTOR_TOKENS_FILE",
                                    value=(
                                        f"{ACTOR_TOKENS_MOUNT_PATH}/"
                                        f"{ACTOR_TOKENS_FILENAME}"
                                    ),
                                ),
                                # Module-level fallback OmnigraphClient's target
                                # (ADR-0004 D4) — omnigraph-server's in-cluster
                                # address.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_MEMORY_URI",
                                    value=omnigraph_server_addr,
                                ),
                                # An http(s) store is addressed as
                                # `--server <url> --graph <id>`, and the graph id
                                # is not encoded in WITAN_MEMORY_URI (a bare
                                # server URL), so it comes from here. Sourced
                                # from the omnigraph stack's own output rather
                                # than a literal: witan must ask for exactly the
                                # graph that stack declared in cluster.yaml, or
                                # it addresses a graph the cluster never created.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_MEMORY_GRAPH",
                                    value=council_graph_id,
                                ),
                                # The code-graph data tier — the same
                                # omnigraph-server, whose `code-<repo>` graphs
                                # data_tier.py declares alongside `council`.
                                # WITAN_CODE_INDEX_ROLE is deliberately left at
                                # its default (`client`): that is what keeps a
                                # write arriving through the MCP boundary from
                                # claiming a graph's shared default-branch view.
                                # Only the in-cluster CI indexer Job declares
                                # itself `ci`.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_CODE_SERVER",
                                    value=omnigraph_server_addr,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_MEMORY_TOKEN",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=witan_ci_token_secret_name,
                                            key=witan_ci_token_secret_key,
                                        )
                                    ),
                                ),
                                # The tier's own credential against the code
                                # graphs, for the questions asked *about* the
                                # server rather than of a graph: `omnigraph
                                # graphs list`, which `ensure_store` runs to
                                # check the cluster actually declares a graph
                                # before a write starts, and which backs
                                # `code_indexed_repos`. That listing is
                                # server-scoped (Cedar `graph_list`) and belongs
                                # to no actor, so it authenticates as the service
                                # or not at all.
                                #
                                # It is NOT what a caller's records are written
                                # under: `witan_code.ingest._client` resolves the
                                # actor from the request's JWT and swaps in that
                                # actor's token before any read or mutation,
                                # refusing outright when the actor has none.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="WITAN_CODE_TOKEN",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=witan_code_token_secret_name,
                                            key=witan_code_token_secret_key,
                                        )
                                    ),
                                ),
                                # The DSN is Vault-synced rather than a literal
                                # like the rest of `otel_env` below: it is
                                # owned by the ol-infrastructure-sentry stack,
                                # not this one, so it travels through Vault the
                                # same way dagster's does.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SENTRY_DSN",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=sentry_dsn_secret_name,
                                            key=sentry_dsn_secret_key,
                                        )
                                    ),
                                ),
                                # Client-side write admission, per graph, inside
                                # this pod. Only emitted when the stack sets a
                                # value: witan's own defaults are the measured
                                # ones, and an env var present-but-empty would
                                # only invite a debate about which layer's
                                # default is in force.
                                *(
                                    [
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_REMOTE_WRITE_MAX_INFLIGHT",
                                            value=str(remote_write_max_inflight),
                                        )
                                    ]
                                    if remote_write_max_inflight
                                    else []
                                ),
                                *(
                                    [
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_REMOTE_WRITE_QUEUE_SECONDS",
                                            value=str(remote_write_queue_seconds),
                                        )
                                    ]
                                    if remote_write_queue_seconds
                                    else []
                                ),
                                # The deadline THIS deployment imposes, told to
                                # witan so it can refuse a write it has no time
                                # left to finish. Must track
                                # WITAN_REQUEST_TIMEOUT above.
                                *(
                                    [
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_REMOTE_CALL_BUDGET_SECONDS",
                                            value=str(remote_call_budget_seconds),
                                        )
                                    ]
                                    if remote_call_budget_seconds
                                    else []
                                ),
                                # Structured logging + OTel, from the shared
                                # helper so this workload and the CI indexer
                                # cannot drift into describing themselves as
                                # two different services. `otel_env` is empty
                                # in CI, which has no collector. `sentry_env`
                                # is this workload's own addition, not shared
                                # with the CI indexer -- it carries no
                                # CI-empty case since one Sentry project covers
                                # every environment.
                                *(
                                    kubernetes.core.v1.EnvVarArgs(
                                        name=name, value=value
                                    )
                                    for name, value in (
                                        witan_log_env()
                                        | otel_env(stack_info, "witan", service_version)
                                        | sentry_env(stack_info, service_version)
                                    ).items()
                                ),
                            ],
                            # See WITAN_HEALTH_PATH: shallow on purpose, and the
                            # startupProbe is what lets liveness and readiness
                            # carry no initial delay of their own.
                            startup_probe=kubernetes.core.v1.ProbeArgs(
                                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                    path=WITAN_HEALTH_PATH,
                                    port=WITAN_PORT,
                                ),
                                initial_delay_seconds=(
                                    WITAN_STARTUP_INITIAL_DELAY_SECONDS
                                ),
                                period_seconds=WITAN_STARTUP_PERIOD_SECONDS,
                                failure_threshold=WITAN_STARTUP_FAILURE_THRESHOLD,
                                timeout_seconds=WITAN_STARTUP_TIMEOUT_SECONDS,
                            ),
                            # Keep serving until APISIX has stopped routing
                            # here. See WITAN_PRESTOP_DRAIN_SECONDS: without
                            # this, SIGTERM lands while the pod is still in the
                            # data plane's upstream set and one read per rollout
                            # came back a 502. This delays SIGTERM only; it does
                            # not extend the drain that follows it.
                            lifecycle=kubernetes.core.v1.LifecycleArgs(
                                pre_stop=kubernetes.core.v1.LifecycleHandlerArgs(
                                    sleep=kubernetes.core.v1.SleepActionArgs(
                                        seconds=WITAN_PRESTOP_DRAIN_SECONDS,
                                    ),
                                ),
                            ),
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                    path=WITAN_HEALTH_PATH,
                                    port=WITAN_PORT,
                                ),
                                # Explicit 0 rather than omitted: a merge that
                                # only sets the fields it names leaves a
                                # previously set initialDelaySeconds in place.
                                initial_delay_seconds=0,
                                period_seconds=WITAN_READINESS_PERIOD_SECONDS,
                                # ~30s of unresponsiveness before this singleton
                                # is pulled out of the Service and the endpoint
                                # disappears from APISIX. See the block above:
                                # at 1s x 3 it ejected the only replica under
                                # ordinary write load and every caller got a 503
                                # while their writes were committing.
                                failure_threshold=WITAN_READINESS_FAILURE_THRESHOLD,
                                timeout_seconds=WITAN_READINESS_TIMEOUT_SECONDS,
                            ),
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                    path=WITAN_HEALTH_PATH,
                                    port=WITAN_PORT,
                                ),
                                # ★ 20s period against the default
                                # failureThreshold of 3, so a wedged process is
                                # killed in ~60s.
                                #
                                # ★ AN EARLIER VERSION OF THIS COMMENT CLAIMED A
                                # SATURATED PROCESS "IS NEVER KILLED AT ALL",
                                # because the probe cannot block on the graph.
                                # That was wrong, and QA proved it on
                                # 2026-08-16: at the inherited 1s timeout this
                                # liveness probe ALSO failed under load, leaving
                                # the pod ~60s from a restart it did not need.
                                # Shallowness stops the handler stalling; it does
                                # not stop a starved event loop from failing to
                                # schedule it. Hence the explicit 10s below.
                                initial_delay_seconds=0,
                                period_seconds=20,
                                timeout_seconds=WITAN_LIVENESS_TIMEOUT_SECONDS,
                            ),
                            resources=witan_resources(
                                cpu_request=cpu_request,
                                memory_request=memory_request,
                                memory_limit=memory_limit,
                            ),
                            security_context=kubernetes.core.v1.SecurityContextArgs(
                                allow_privilege_escalation=False,
                                privileged=False,
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                run_as_user=WITAN_RUN_AS,
                                run_as_group=WITAN_RUN_AS,
                                capabilities=kubernetes.core.v1.CapabilitiesArgs(
                                    drop=["ALL"],
                                ),
                                seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                                    type="RuntimeDefault",
                                ),
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="actor-tokens",
                                    mount_path=ACTOR_TOKENS_MOUNT_PATH,
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="tmp",
                                    mount_path=TMP_MOUNT_PATH,
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="actor-tokens",
                            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                secret_name=actor_tokens_secret_name,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="tmp",
                            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                                size_limit=TMP_SIZE_LIMIT,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        # `migration_job` makes the data migrations a genuine pre-deploy gate:
        # pulumi-kubernetes awaits a Job's completion, so the new image is not
        # started until the backfills for it have succeeded, and a failed
        # migration blocks the rollout instead of half-applying it.
        opts=ResourceOptions(
            depends_on=[
                witan_service_account,
                witan_ci_token_secret,
                witan_code_token_secret,
                sentry_dsn_secret,
                actor_tokens_secret,
                migration_job,
            ]
        ),
    )

    witan_service = kubernetes.core.v1.Service(
        f"witan-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=WITAN_SERVICE_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            selector={"app.kubernetes.io/name": WITAN_SERVICE_NAME},
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="http",
                    port=WITAN_PORT,
                    target_port=WITAN_PORT,
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
        opts=ResourceOptions(depends_on=[witan_deployment]),
    )

    # The deadline, made explicit. Without this the upstream timeout is whatever
    # APISIX defaults to — inherited, invisible from this stack, and free to
    # change under us on a gateway upgrade. See WITAN_REQUEST_TIMEOUT.
    #
    # `BackendTrafficPolicy` targets the SERVICE, not the HTTPRoute: it is the
    # apisix.apache.org/v1alpha1 lever the ingress controller (2.1.0 here)
    # offers for Gateway API backends, since HTTPRoute's own `rules[].timeouts`
    # are not honoured by it.
    kubernetes.apiextensions.CustomResource(
        f"witan-backend-traffic-policy-{stack_info.env_suffix}",
        api_version="apisix.apache.org/v1alpha1",
        kind="BackendTrafficPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="witan-timeouts",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "targetRefs": [
                {
                    "group": "",
                    "kind": "Service",
                    "name": WITAN_SERVICE_NAME,
                }
            ],
            "timeout": {
                "connect": WITAN_CONNECT_TIMEOUT,
                "send": WITAN_SEND_TIMEOUT,
                "read": WITAN_REQUEST_TIMEOUT,
            },
        },
        opts=ResourceOptions(depends_on=[witan_service]),
    )

    return WitanServingTier(
        deployment=witan_deployment,
        service=witan_service,
    )
