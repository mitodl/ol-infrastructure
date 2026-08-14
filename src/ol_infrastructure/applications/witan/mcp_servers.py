"""Backend MCP server definition for witan.

The ``witan-tools`` ``MCPGroup`` exists so the ``VirtualMCPServer`` in
``__main__.py`` can front it behind a single endpoint, following the
``toolhive_swe`` pattern. witan is the group's only member, and is expected to
stay that way: the code-graph tools are mounted **in-process** by ``witan
serve`` (``witan_mcp.mount(code_mcp)``, agent-kit
``mcp/servers/witan/witan/cli/__init__.py``), not run as a separate workload,
and there is no intent to split them out. The group and the vMCP earn their
keep as the OIDC and ingress boundary, not as an aggregation point.

An earlier version of this docstring justified the group as leaving room for a
second backend. That framing is what led ``__main__.py`` to accept the CRD's
default ``conflictResolution: prefix``, which renamed every tool to
``witan_*`` and broke every client. Aggregation config there is now set for
the single-backend reality; see the comment on ``config.aggregation``.

Unlike every backend in ``toolhive_swe`` (fetch/grafana/context7/sentry, which
carry no identity of their own and trust the vMCP's auth wholesale), witan
does its own direct JWT validation (agent-kit ADR-0004 D1) and needs the
*original* Keycloak-issued bearer token to reach its container unmodified —
not a vMCP-embedded-auth-server swap token. That is why ``__main__.py``
configures the ``VirtualMCPServer`` with ToolHive's "External OIDC provider"
scenario (no ``authServerConfig``) instead of copying ``toolhive_swe``'s
embedded-broker config; see the module docstring there for the full
rationale.

Deliberately NOT setting ``spec.oidcConfigRef`` on the ``witan`` MCPServer
itself (unlike the CRD's own recommendation for defense-in-depth): ToolHive
requires each ``MCPOIDCConfigReference.audience`` to be unique per resource
that references it, and witan's own ``JWTVerifier`` already validates
``WITAN_OIDC_AUDIENCE`` against the *same* forwarded token the vMCP validated
moments earlier — a second, differently-audienced validation hop here would
require Keycloak to mint a multi-audience token for no proven benefit yet.
The vMCP → backend hop is ClusterIP-only, never internet-reachable, the same
trust boundary every existing ``toolhive_swe`` backend already relies on.
Revisit if that stops being true (e.g. a future backend outside this
namespace).
"""

from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions, StackReference

from ol_infrastructure.applications.witan.observability import (
    downward_api_env_dicts,
    otel_env,
    witan_log_env,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.toolhive_telemetry import (
    toolhive_mcpserver_audit,
    toolhive_service_name,
)

# Name shared by the MCPGroup and the VirtualMCPServer that references it.
MCP_GROUP_NAME = "witan-tools"

# Prefix for this stack's ToolHive telemetry CR names and OTel service names.
# Equal to the namespace today; kept as its own name because one is a
# Kubernetes object and the other is a Grafana-facing identity, and renaming
# either should not silently rename the other.
TOOLHIVE_SERVICE = "witan"

# The MCPServer resource name. ToolHive derives a workload's backend id from the
# resource name, so this is also the key the vMCP's `outgoingAuth.backends`
# map must use. A key that doesn't match resolves to no per-backend strategy at
# all, which lands on the unauthenticated one — the exact failure the "Outgoing
# auth" section of `__main__.py`'s docstring describes. Shared as a constant so
# the two cannot drift.
WITAN_MCPSERVER_NAME = "witan"

# Mount path (inside the witan container) for the actor-tokens Secret volume.
# The MCPServer CRD's own `volumes` field only supports hostPath mounts, so
# this is wired via `spec.podTemplateSpec` (RawExtension) instead — see
# https://docs.stacklok.com/toolhive/reference/crd/mcpserver, MCPServerSpec.
ACTOR_TOKENS_MOUNT_PATH = "/etc/witan/actor-tokens"  # pragma: allowlist secret
ACTOR_TOKENS_FILENAME = "tokens.json"  # pragma: allowlist secret

# Writable scratch space. The container runs `readOnlyRootFilesystem: true`
# with no writable volume, so Python's tempfile found nothing in
# ['/tmp', '/var/tmp', '/usr/tmp', '/src'] and every server-side tool that
# needs a temp file failed outright — verified against CI on 2026-08-07, where
# `witan migrate merge` through the MCP tier (ADR-0007 D5) returned
# "No usable temporary directory found" at every payload size, including
# --dry-run. Those tools hand a file to the `omnigraph` binary, so there is no
# in-memory path to fall back to.
#
# An emptyDir rather than relaxing readOnlyRootFilesystem: the read-only root
# is worth keeping, and this grants exactly the one writable directory needed.
# Disk-backed on purpose — `medium: Memory` would charge this to the pod's
# 512Mi memory limit and OOM the server on a large export.
#
# Writability depends on `fsGroup`: kubelet group-owns an emptyDir by it, and
# the container runs as uid/gid 1000, so without it the mount is root-owned and
# useless — the same trap ci_indexer.py documents on its scratch volume.
#
# The ToolHive operator already sets `fsGroup: 1000` (verified on the running
# pod in CI, QA and Production), so TMP_FS_GROUP below is a no-op today. It is
# declared anyway: that value is the operator's default, not something this
# stack asked for, and an upgrade that changed it would turn this mount
# read-only with no signal beyond witan tools failing again. Declaring it makes
# the invariant ours instead of inherited.
TMP_MOUNT_PATH = "/tmp"  # noqa: S108
TMP_FS_GROUP = 1000

# ToolHive v0.40.1 accepts `spec.resources` and never applies it to the backend
# container. Verified 2026-08-07 against all three environments: this MCPServer
# declares 500m/512Mi and `kubectl get sts witan -o jsonpath=
# '{.spec.template.spec.containers[0].resources}'` returns `{}`. It is not the
# podTemplateSpec below shadowing it — toolhive-swe/aws has NO podTemplateSpec at
# all, declares 200m/256Mi, and renders `{}` just the same.
#
# The consequence is not cosmetic: with no requests the pod is **BestEffort QoS**,
# first in line for eviction under node memory pressure, and with no limit a
# runaway server can take its node's other pods with it. It also makes the CRD
# actively misleading — an audit that reads `spec.resources` concludes the backend
# is bounded when nothing bounds it.
#
# So the same values are restated on the `mcp` container in `podTemplateSpec`,
# which the operator demonstrably DOES honour (that is how env and volumeMounts
# arrive). `spec.resources` is deliberately left in place as the declaration of
# intent for whenever upstream wires it up; the two must be kept in step, hence
# one constant feeding both.
#
# Tracked as tk-toolhive-operator-drops-mcpserver-spec-resources-8ea1ff.
# ★ Verify any change here against the RENDERED StatefulSet, never the CRD.
WITAN_BACKEND_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "256Mi"},
    "limits": {"cpu": "500m", "memory": "512Mi"},
}
# Sized for the server-side work, which is dominated by the graph export
# `store_merge` takes of its OWN target to reconcile against — that grows with
# the shared graph, not with the caller's upload (the client's batches are
# capped near 2 MiB by witan_core.chunking.MCP_LOAD_MAX_BYTES). 2Gi is well
# clear of a council graph's export today and far below the indexer's 8Gi.
TMP_SIZE_LIMIT = "2Gi"

# ── Proxy-runner health checking ─────────────────────────────────────────────
#
# WHY THIS BLOCK EXISTS: without it, a burst of concurrent WRITES takes the
# whole service down — readers included — and it is not a crash, a leak, or a
# resource limit. Observed live in CI on 2026-08-12 at 16 concurrent
# `memory_store` calls, and the chain runs entirely through health checking:
#
#   1. witan serialises writes on one graph at ~2s each, so the queue outlives
#      any single request.
#   2. The proxy runner's `/health` handler SYNCHRONOUSLY pings the MCP backend
#      (upstream `pkg/healthcheck/healthcheck.go:CheckHealth`). A saturated
#      backend does not answer, so `/health` hangs rather than returning.
#   3. The kubelet's liveness probe on the proxy container — `/health`,
#      `timeoutSeconds: 5`, `failureThreshold: 3` — kills the container.
#      ("Container toolhive failed liveness probe, will be restarted", with
#      `lastState.terminated{exitCode: 0, reason: Completed}`.)
#   4. While it restarts, `mcp-witan-proxy:8080` has no ready endpoint, so the
#      vMCP's own backend check gets `connect: connection refused`, marks the
#      only backend unhealthy, and terminates EVERY session with "no backends
#      returned capabilities". ~60s of total outage from a 30s write burst.
#
# ★ THE ROOT CAUSE IS TWO CONSTANTS THAT HAPPEN TO BE EQUAL. Upstream's
# `DefaultPingerTimeout` is 5s (pkg/transport/proxy/transparent/pinger.go:26)
# and the liveness probe the SAME repo generates uses `timeoutSeconds: 5`
# (cmd/thv-operator/controllers/mcpserver_controller.go). The inner ping and the
# outer probe expire together, so the probe can never win that race.
#
# ★ AND THE FIX IS FREE, BECAUSE A DEGRADED BACKEND STILL ANSWERS 200.
# `healthcheck.go` maps StatusDegraded to `http.StatusOK` ("Still return 200 for
# degraded state"); only StatusUnhealthy is 503. So the container is killed when
# `/health` HANGS, never when it reports the backend degraded. Making the ping
# give up well inside the probe's 5s budget keeps liveness green through exactly
# the load that currently kills it, and costs nothing when the backend is fine.
#
# BOTH values are required and shipping only the first is worse than shipping
# neither. The same pinger drives the proxy's own internal health loop, which
# initiates SHUTDOWN after `FAILURE_THRESHOLD` consecutive failures at
# `DefaultHealthCheckInterval` (10s). A shorter ping timeout makes that loop
# fail sooner, so the threshold must rise to compensate — otherwise the proxy
# trades a liveness kill for a self-inflicted shutdown that looks identical from
# outside. 12 x 10s tolerates ~120s of backend saturation, against the ~60s
# window actually observed. A genuinely dead backend is still noticed, and
# restarting the PROXY was never going to revive it anyway — the backend is a
# different pod.
#
# ★ INVALID VALUES ARE IGNORED SILENTLY. Upstream parses these with
# `time.ParseDuration` / `strconv.Atoi` and falls back to the default with only
# a WARN log, so a typo here reads as "configured" while changing nothing.
# Verify against the running proxy, not against this file.
#
# Delivered through `spec.resourceOverrides.proxyDeployment.env`, which is the
# ONLY lever available: the probes themselves are hardcoded by the operator
# (`ProxyDeploymentOverrides` carries annotations, labels, podTemplateMetadata,
# env and imagePullSecrets — no probe fields), and the `podTemplateSpec` used
# above patches the MCP workload's StatefulSet, not this Deployment.
#
# Tracked as tk-a-write-burst-takes-the-whole-deployed-witan-dow-fc2ce7.
# The underlying write throughput is a separate problem — see
# tk-upstream-omnigraph-a-single-row-insert-costs-a-f-eeeae3. This change makes
# saturation degrade instead of outage; it does not make writes faster.
WITAN_PROXY_HEALTH_ENV = [
    # Upstream default 5s, equal to the liveness probe's timeoutSeconds.
    {"name": "TOOLHIVE_HEALTH_CHECK_PING_TIMEOUT", "value": "2s"},
    # Upstream default 5, i.e. ~50s at the 10s check interval.
    {"name": "TOOLHIVE_HEALTH_CHECK_FAILURE_THRESHOLD", "value": "12"},
]


class WitanMCPServers(NamedTuple):
    """Handles to the group and backend server CRs for depends_on wiring."""

    group: kubernetes.apiextensions.CustomResource
    servers: list[kubernetes.apiextensions.CustomResource]


def create_mcp_servers(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    cluster_stack: StackReference,
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
    migration_job: Resource,
    service_version: str,
    telemetry_config_name: str,
    telemetry_config: Resource,
    remote_write_max_inflight: str = "",
    remote_write_queue_seconds: str = "",
    remote_call_budget_seconds: str = "",
) -> WitanMCPServers:
    """Provision the witan-tools MCPGroup and the witan MCPServer backend.

    ``telemetry_config_name`` is the ``MCPTelemetryConfig`` this hop references.
    Unlike the vMCP, this hop has one in EVERY environment: the Prometheus
    ``/metrics`` path is safe on its ClusterIP-only proxy port, and in CI — which
    has no OTLP receiver — it is the only instrumentation there is. See
    ``lib.toolhive_telemetry.toolhive_telemetry_spec``.

    ``remote_write_max_inflight`` / ``remote_write_queue_seconds`` retune
    witan's client-side write admission. Empty (the default) leaves witan's own
    defaults in force — the env var is omitted entirely rather than set to an
    empty string, so "unset" and "set to nothing" cannot diverge between what
    Pulumi declares and what witan reads.

    ``remote_call_budget_seconds`` tells witan how long a tool call has here
    before ToolHive stops waiting for it, so it can refuse a write it cannot
    finish rather than be cut off mid-call. witan-core assumes no deadline of
    its own — the same library runs from a CLI and from a batch Job — so this
    is the deployment declaring one. See the note at its call site.
    """
    witan_mcpgroup = kubernetes.apiextensions.CustomResource(
        f"witan-mcpgroup-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPGroup",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=MCP_GROUP_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "description": (
                "witan MCP workloads aggregated behind the witan VirtualMCPServer"
            ),
        },
        opts=ResourceOptions(depends_on=[cluster_stack]),
    )

    # witan's own FastMCP process. `--transport streamable-http` is what
    # ToolHive hosts (witan/cli/__init__.py:67-68, agent-kit repo). The data
    # tier (omnigraph-server) is reached over the cluster network only — see
    # data_tier.py — never exposed via this MCPServer directly.
    witan_mcpserver = kubernetes.apiextensions.CustomResource(
        f"witan-mcpserver-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPServer",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=WITAN_MCPSERVER_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "image": witan_image,
            "transport": "streamable-http",
            "proxyPort": 8080,
            "mcpPort": 8000,
            "groupRef": {"name": MCP_GROUP_NAME},
            "args": [
                "serve",
                "--transport",
                "streamable-http",
                "--host",
                "0.0.0.0",  # noqa: S104
                "--port",
                "8000",
            ],
            "env": [
                # Direct OIDC/JWT validation against Keycloak (ADR-0004 D1) —
                # witan is the identity boundary here, not ToolHive.
                {"name": "WITAN_OIDC_ISSUER", "value": oidc_issuer},
                {"name": "WITAN_OIDC_AUDIENCE", "value": oidc_audience},
                {
                    "name": "WITAN_ACTOR_TOKENS_FILE",
                    "value": f"{ACTOR_TOKENS_MOUNT_PATH}/{ACTOR_TOKENS_FILENAME}",
                },
                # Module-level fallback OmnigraphClient's target (ADR-0004
                # D4) — the omnigraph-server Deployment's in-cluster address.
                {"name": "WITAN_MEMORY_URI", "value": omnigraph_server_addr},
                # An http(s) store is addressed as `--server <url> --graph
                # <id>`, and the graph id is not encoded in WITAN_MEMORY_URI
                # (a bare server URL), so it comes from here. Sourced from the
                # omnigraph stack's own `council_graph_id` output rather than
                # a literal, for the same reason the address is: witan must
                # ask for exactly the graph that stack declared in cluster.yaml
                # or it addresses a graph the cluster never created. (It also
                # happens to equal witan's built-in `council` default, but
                # relying on two independent defaults agreeing is the failure
                # mode this avoids.)
                {"name": "WITAN_MEMORY_GRAPH", "value": council_graph_id},
                # The code-graph data tier — the same omnigraph-server, whose
                # `code-<repo>` graphs data_tier.py declares alongside
                # `council`. Without it the tier serves code-graph reads out of
                # whatever `code_dir` the container has (nothing) and can serve
                # no cluster writes at all, so the `code_store_*` tools it
                # registers for remote indexers (agent-kit ADR-0005 path c)
                # have nowhere to write.
                #
                # No graph id counterpart to WITAN_MEMORY_GRAPH here: a code
                # graph is addressed per repo, and witan-code derives the id
                # from the canonical repo URI the caller names
                # (`witan_code.config.graph_id`, the byte-for-byte mirror of
                # `data_tier.code_graph_id`). A graph this cluster does not
                # declare fails loudly on the first store call.
                #
                # WITAN_CODE_INDEX_ROLE is deliberately left at its default
                # (`client`). It is what keeps a write arriving through the MCP
                # boundary from claiming a graph's shared default-branch view;
                # only the in-cluster CI indexer Job declares itself `ci`.
                {"name": "WITAN_CODE_SERVER", "value": omnigraph_server_addr},
                # Client-side write admission, per graph, inside this pod —
                # the global bound the data tier's PER-ACTOR cap cannot be,
                # since every user's write funnels through this single replica
                # and the 30s deadline sees total concurrency, not one actor's
                # share. Only emitted when the stack sets a value: witan's own
                # defaults (4 in flight, 10s of queue) are the measured ones,
                # and an env var present-but-empty would only invite a debate
                # about which layer's default is in force.
                *(
                    [
                        {
                            "name": "WITAN_REMOTE_WRITE_MAX_INFLIGHT",
                            "value": str(remote_write_max_inflight),
                        }
                    ]
                    if remote_write_max_inflight
                    else []
                ),
                *(
                    [
                        {
                            "name": "WITAN_REMOTE_WRITE_QUEUE_SECONDS",
                            "value": str(remote_write_queue_seconds),
                        }
                    ]
                    if remote_write_queue_seconds
                    else []
                ),
                # The deadline THIS deployment imposes, told to witan so it can
                # refuse a write it has no time left to finish. Set here rather
                # than defaulted in witan-core because it is a fact about
                # ToolHive, not about the client library — see the call site.
                *(
                    [
                        {
                            "name": "WITAN_REMOTE_CALL_BUDGET_SECONDS",
                            "value": str(remote_call_budget_seconds),
                        }
                    ]
                    if remote_call_budget_seconds
                    else []
                ),
                # Structured logging + OTel. Appended from a shared helper
                # rather than spelled out here so this workload, the CI
                # indexer, and anything added later cannot drift into
                # describing themselves as three different services. `otel_env`
                # is empty in CI, which has no collector — see observability.py.
                *(
                    {"name": name, "value": value}
                    for name, value in (
                        witan_log_env() | otel_env(stack_info, "witan", service_version)
                    ).items()
                ),
            ],
            "secrets": [
                {
                    "name": witan_ci_token_secret_name,
                    "key": witan_ci_token_secret_key,
                    "targetEnvName": "WITAN_MEMORY_TOKEN",
                },
                # The tier's own credential against the code graphs, for the
                # questions asked *about* the server rather than of a graph:
                # `omnigraph graphs list`, which `ensure_store` runs to check
                # the cluster actually declares a graph before a write starts
                # (a provisioning gap becomes one clear refusal instead of an
                # error per record), and which backs `code_indexed_repos`. That
                # listing is server-scoped (Cedar `graph_list`) and belongs to
                # no actor, so it authenticates as the service or not at all —
                # and omnigraph-server, booted with a bearer-tokens file,
                # resolves no actor from an absent token and denies it.
                #
                # It is NOT what a caller's records are written under.
                # `witan_code.ingest._client` resolves the actor from the
                # request's JWT and swaps in that actor's token from
                # WITAN_ACTOR_TOKENS_FILE before any read or mutation, refusing
                # outright when the actor has none — serving a caller under the
                # service identity is what that layer exists to prevent
                # (agent-kit ADR-0005 path c).
                #
                # Its own Secret rather than a second entry against
                # witan-ci-token, whose value it currently duplicates: this
                # list is keyed by secret name and rejects two entries naming
                # one Secret. See WITAN_CODE_TOKEN_SECRET_NAME in __main__.py —
                # svc-witan-ci is borrowed here, the same way migrations.py
                # borrows it, until the `witan-service`/`act-svc-witan` account
                # witan's Cedar bundle already models is provisioned. Sharing
                # it grants the tier no write it could not otherwise make: the
                # actor swap above is unconditional, and WITAN_CODE_INDEX_ROLE
                # stays `client`, so the CI role's one real privilege — writing
                # a graph's shared default-branch view — stays unreachable.
                {
                    "name": witan_code_token_secret_name,
                    "key": witan_code_token_secret_key,
                    "targetEnvName": "WITAN_CODE_TOKEN",
                },
            ],
            # No outbound network needed beyond the in-cluster omnigraph-server
            # Service and the Keycloak JWKS endpoint (JWT validation).
            "permissionProfile": {
                "type": "builtin",
                "name": "network",
            },
            # Declared, but NOT what actually reaches the container — the
            # operator drops it. See WITAN_BACKEND_RESOURCES.
            "resources": WITAN_BACKEND_RESOURCES,
            # Keeps a write burst from getting the proxy container killed by its
            # own liveness probe, which is what turns backend saturation into a
            # service-wide outage. See WITAN_PROXY_HEALTH_ENV.
            "resourceOverrides": {
                "proxyDeployment": {"env": WITAN_PROXY_HEALTH_ENV},
            },
            # The proxy's own OTel pipeline — spans and metrics for the hop in
            # FRONT of witan, which was dark until 2026-08-14. The SPANS are the
            # point: only nested span timestamps separate the pre-handler
            # interval from the post-response one per request. The metric delta
            # against witan's `duration_ms` bounds the two together and only in
            # aggregate. See observability.py's "ToolHive tier" section.
            #
            # Deliberately NO `k8s.grafana.com/scrape` annotations to go with the
            # Prometheus path this enables: in QA and Production the same metrics
            # already arrive over OTLP, and scraping them as well would ingest
            # every series twice under one name. The path exists for a
            # port-forward — in CI, where there is no receiver, that is the only
            # way to read them at all.
            "telemetryConfigRef": {
                "name": telemetry_config_name,
                "serviceName": toolhive_service_name(
                    stack_info, TOOLHIVE_SERVICE, "mcp-proxy"
                ),
            },
            # Per-request JSON to stdout, so it rides the existing pod-log path
            # to Loki with no collector involved — which is why this is on in CI
            # too. This CRD accepts only `enabled`; see `toolhive_mcpserver_audit`
            # for why nothing else may be written here.
            "audit": toolhive_mcpserver_audit(),
            # `volumes`/`volumeMounts` aren't first-class MCPServerSpec fields
            # beyond hostPath, so the actor-tokens Secret is mounted via the
            # documented escape hatch: a PodTemplateSpec merge-patch targeting
            # the operator-managed `mcp` container by name.
            "podTemplateSpec": {
                "spec": {
                    "containers": [
                        {
                            "name": "mcp",
                            # Pod identity for spans and log lines. It rides
                            # the same patch as the volume mount because
                            # `spec.env` above is name/value only and cannot
                            # express a `fieldRef` — the operator merges full
                            # EnvVars from here onto the `mcp` container, which
                            # is already how `spec.secrets` arrives as
                            # `secretKeyRef`.
                            "env": downward_api_env_dicts(),
                            # The resources that actually take effect — the
                            # operator ignores `spec.resources` above. Without
                            # this the container renders `resources: {}` and
                            # runs BestEffort. See WITAN_BACKEND_RESOURCES.
                            "resources": WITAN_BACKEND_RESOURCES,
                            "volumeMounts": [
                                {
                                    "name": "actor-tokens",
                                    "mountPath": ACTOR_TOKENS_MOUNT_PATH,
                                    "readOnly": True,
                                },
                                {
                                    "name": "tmp",
                                    "mountPath": TMP_MOUNT_PATH,
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "actor-tokens",
                            "secret": {"secretName": actor_tokens_secret_name},
                        },
                        {
                            "name": "tmp",
                            "emptyDir": {"sizeLimit": TMP_SIZE_LIMIT},
                        },
                    ],
                    # What makes the emptyDir above writable by a non-root
                    # container — see TMP_FS_GROUP.
                    "securityContext": {"fsGroup": TMP_FS_GROUP},
                }
            },
        },
        # Wait for the secrets this MCPServer consumes (witan-ci-token and
        # witan-code-token via spec.secrets, actor-tokens via podTemplateSpec)
        # so the operator
        # doesn't reconcile it into a pending pod before they exist — the same
        # secret-in-depends_on wiring toolhive_swe uses for its backends.
        #
        # `migration_job` makes the data migrations a genuine pre-deploy gate:
        # pulumi-kubernetes awaits a Job's completion, so the operator is not
        # handed the new image until the backfills for it have succeeded, and a
        # failed migration blocks the rollout instead of half-applying it.
        #
        # `telemetry_config` for the same reason as the secrets: the operator
        # resolves `telemetryConfigRef` by name at reconcile time, and a
        # reference to a CR that does not exist yet is a degraded server rather
        # than a retry.
        opts=ResourceOptions(
            depends_on=[
                witan_mcpgroup,
                witan_ci_token_secret,
                witan_code_token_secret,
                actor_tokens_secret,
                migration_job,
                telemetry_config,
            ]
        ),
    )

    return WitanMCPServers(
        group=witan_mcpgroup,
        servers=[witan_mcpserver],
    )
