"""Backend MCP server definition for witan.

The ``witan-tools`` ``MCPGroup`` exists so the ``VirtualMCPServer`` in
``__main__.py`` can front the group behind a single endpoint, following the
``toolhive_swe`` pattern even though — today — witan is the group's only
member. This leaves room to add a second backend (e.g. a dedicated
``witan-code`` workload) later without restructuring the ingress/auth layer.

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

from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and the VirtualMCPServer that references it.
MCP_GROUP_NAME = "witan-tools"

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
) -> WitanMCPServers:
    """Provision the witan-tools MCPGroup and the witan MCPServer backend."""
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
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
            # `volumes`/`volumeMounts` aren't first-class MCPServerSpec fields
            # beyond hostPath, so the actor-tokens Secret is mounted via the
            # documented escape hatch: a PodTemplateSpec merge-patch targeting
            # the operator-managed `mcp` container by name.
            "podTemplateSpec": {
                "spec": {
                    "containers": [
                        {
                            "name": "mcp",
                            "volumeMounts": [
                                {
                                    "name": "actor-tokens",
                                    "mountPath": ACTOR_TOKENS_MOUNT_PATH,
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "actor-tokens",
                            "secret": {"secretName": actor_tokens_secret_name},
                        }
                    ],
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
        opts=ResourceOptions(
            depends_on=[
                witan_mcpgroup,
                witan_ci_token_secret,
                witan_code_token_secret,
                actor_tokens_secret,
                migration_job,
            ]
        ),
    )

    return WitanMCPServers(
        group=witan_mcpgroup,
        servers=[witan_mcpserver],
    )
