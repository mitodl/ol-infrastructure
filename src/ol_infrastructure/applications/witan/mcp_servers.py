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
from pulumi import Output, ResourceOptions, StackReference

from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and the VirtualMCPServer that references it.
MCP_GROUP_NAME = "witan-tools"

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
    oidc_issuer: str,
    oidc_audience: str,
    actor_tokens_secret_name: str,
    witan_ci_token_secret_name: str,
    witan_ci_token_secret_key: str,
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
            name="witan",
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
            ],
            "secrets": [
                {
                    "name": witan_ci_token_secret_name,
                    "key": witan_ci_token_secret_key,
                    "targetEnvName": "WITAN_MEMORY_TOKEN",
                }
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
        opts=ResourceOptions(depends_on=[witan_mcpgroup]),
    )

    return WitanMCPServers(
        group=witan_mcpgroup,
        servers=[witan_mcpserver],
    )
