"""Backend MCP server definitions for the SWE agent class.

The ``MCPGroup`` (``swe-tools``) groups the SWE backend MCP servers so a
``VirtualMCPServer`` can aggregate them behind a single endpoint. Backends join
by setting ``spec.groupRef.name`` to the group's name; the ToolHive operator
reconciles each ``MCPServer`` into a proxy Deployment + Service reachable
in-cluster (e.g. ``http://mcp-fetch-proxy.<namespace>.svc.cluster.local:8080/mcp``).

This is the module that grows as new tools are added to the SWE group: define
each backend ``MCPServer`` here and append it to the ``servers`` list returned
by :func:`create_mcp_servers` so the vMCP's ``depends_on`` wiring picks it up.
"""

from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import MCP_GRAFANA_VERSION, MCP_SENTRY_VERSION
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and every backend/virtual server that references it.
MCP_GROUP_NAME = "swe-tools"

# K8s Secret holding the Grafana Cloud service account token, materialised from
# encrypted stack config and injected into the grafana MCPServer via ToolHive's
# ``spec.secrets`` -> ``targetEnvName`` mechanism.
GRAFANA_TOKEN_SECRET_NAME = "toolhive-swe-grafana-token"  # noqa: S105  # pragma: allowlist secret
GRAFANA_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret

# K8s Secret holding the Sentry user auth token, materialised from encrypted stack
# config and injected into the sentry MCPServer the same way as the Grafana token.
SENTRY_TOKEN_SECRET_NAME = "toolhive-swe-sentry-token"  # noqa: S105  # pragma: allowlist secret
SENTRY_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret


class ToolhiveSWEMCPServers(NamedTuple):
    """Handles to the group and backend server CRs for depends_on wiring."""

    group: kubernetes.apiextensions.CustomResource
    servers: list[kubernetes.apiextensions.CustomResource]


def create_mcp_servers(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    cluster_stack: StackReference,
    toolhive_swe_config: Config,
) -> ToolhiveSWEMCPServers:
    """Provision the MCPGroup and every backend MCPServer that joins it."""
    swe_mcpgroup = kubernetes.apiextensions.CustomResource(
        f"toolhive-swe-mcpgroup-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPGroup",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=MCP_GROUP_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "description": (
                "SWE agent-class MCP servers aggregated behind the swe VirtualMCPServer"
            ),
        },
        opts=ResourceOptions(depends_on=[cluster_stack]),
    )

    # The reference fetch MCP server (example workload). The operator reconciles
    # this into a proxy Deployment + Service (``mcp-fetch-proxy``).
    fetch_mcpserver = kubernetes.apiextensions.CustomResource(
        f"toolhive-swe-fetch-mcpserver-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPServer",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="fetch",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "image": "ghcr.io/stackloklabs/gofetch/server:1.0.5",
            "transport": "streamable-http",
            "proxyPort": 8080,
            "mcpPort": 8080,
            "groupRef": {"name": MCP_GROUP_NAME},
            # Fetch needs outbound network access to retrieve URLs. The "network"
            # builtin profile grants egress; tighten to an allow-list ConfigMap when
            # the set of reachable hosts is known.
            "permissionProfile": {
                "type": "builtin",
                "name": "network",
            },
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "100m", "memory": "128Mi"},
            },
        },
        opts=ResourceOptions(depends_on=[swe_mcpgroup]),
    )

    # Grafana OSS MCP server pointed at Grafana Cloud with a service account token
    # (https://docs.stacklok.com/toolhive/guides-mcp/grafana). Deliberately NOT a
    # proxy to the hosted Grafana Cloud MCP endpoint: that endpoint only supports
    # interactive OAuth 2.1 against Grafana Cloud's own auth server, which would
    # force users through a second browser login on top of the vMCP's Keycloak
    # flow. A token-scoped OSS backend keeps user auth single-hop; the tradeoff is
    # that every user acts as the one service account, so scope its Grafana RBAC
    # permissions accordingly (least privilege, read-mostly).
    #
    # Both values come from stack config:
    #   pulumi config set toolhive_swe:grafana_url https://<stack>.grafana.net
    #   pulumi config set --secret toolhive_swe:grafana_service_account_token -- <token>
    grafana_url = toolhive_swe_config.require("grafana_url")
    grafana_token_secret = kubernetes.core.v1.Secret(
        f"toolhive-swe-grafana-token-secret-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GRAFANA_TOKEN_SECRET_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        type="Opaque",
        string_data={
            GRAFANA_TOKEN_SECRET_KEY: toolhive_swe_config.require_secret(
                "grafana_service_account_token"
            ),
        },
        opts=ResourceOptions(),
    )
    grafana_mcpserver = kubernetes.apiextensions.CustomResource(
        f"toolhive-swe-grafana-mcpserver-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPServer",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="grafana",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "image": f"docker.io/grafana/mcp-grafana:{MCP_GRAFANA_VERSION}",
            "transport": "streamable-http",
            # The image ENTRYPOINT hardcodes ``--transport sse --address
            # 0.0.0.0:8000``; these args are appended after it and Go stdlib flag
            # parsing lets the last occurrence win. Without the override the
            # container serves legacy SSE and the vMCP's streamable-http
            # ``initialize`` POST fails with a 4xx. ``--endpoint-path`` defaults
            # to ``/`` but the ToolHive proxy forwards ``/mcp`` verbatim.
            "args": [
                "--transport",
                "streamable-http",
                "--endpoint-path",
                "/mcp",
            ],
            "proxyPort": 8080,
            "mcpPort": 8000,
            "groupRef": {"name": MCP_GROUP_NAME},
            "env": [{"name": "GRAFANA_URL", "value": grafana_url}],
            "secrets": [
                {
                    "name": GRAFANA_TOKEN_SECRET_NAME,
                    "key": GRAFANA_TOKEN_SECRET_KEY,
                    "targetEnvName": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
                }
            ],
            # Needs outbound access to the Grafana Cloud stack. Tighten to an
            # allow-list profile (grafana_url host, port 443) once the builtin
            # profile proves out.
            "permissionProfile": {
                "type": "builtin",
                "name": "network",
            },
            "resources": {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "200m", "memory": "256Mi"},
            },
        },
        opts=ResourceOptions(depends_on=[swe_mcpgroup, grafana_token_secret]),
    )

    servers = [fetch_mcpserver, grafana_mcpserver]

    # Sentry MCP server (https://github.com/getsentry/sentry-mcp) running in its
    # self-hosted ``stdio`` mode via ToolHive's prebuilt npx wrapper image; the
    # operator's proxy exposes it over streamable-http like the other backends.
    # Every user acts as the one Sentry user auth token, so scope that token's
    # permissions accordingly (least privilege) — see the grafana note above.
    #
    # Gated behind a per-stack boolean so it only runs where explicitly enabled
    # (currently Production only). When disabled neither the token Secret nor the
    # MCPServer is created, so lower stacks don't need a ``sentry_access_token``:
    #   pulumi config set toolhive_swe:sentry_enabled true
    #   pulumi config set --secret toolhive_swe:sentry_access_token -- <token>
    # ``SENTRY_HOST`` is only needed for self-hosted Sentry; left unset we default
    # to the SaaS host (sentry.io). Set it via config to point at a self-hosted
    # install:
    #   pulumi config set toolhive_swe:sentry_host sentry.example.com
    if toolhive_swe_config.get_bool("sentry_enabled"):
        sentry_token_secret = kubernetes.core.v1.Secret(
            f"toolhive-swe-sentry-token-secret-{stack_info.env_suffix}",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=SENTRY_TOKEN_SECRET_NAME,
                namespace=namespace,
                labels=k8s_global_labels,
            ),
            type="Opaque",
            string_data={
                SENTRY_TOKEN_SECRET_KEY: toolhive_swe_config.require_secret(
                    "sentry_access_token"
                ),
            },
            opts=ResourceOptions(),
        )
        sentry_env = []
        sentry_host = toolhive_swe_config.get("sentry_host")
        if sentry_host:
            sentry_env.append({"name": "SENTRY_HOST", "value": sentry_host})
        sentry_mcpserver = kubernetes.apiextensions.CustomResource(
            f"toolhive-swe-sentry-mcpserver-{stack_info.env_suffix}",
            api_version="toolhive.stacklok.dev/v1beta1",
            kind="MCPServer",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="sentry",
                namespace=namespace,
                labels=k8s_global_labels,
            ),
            spec={
                "image": (
                    "ghcr.io/stacklok/dockyard/npx/sentry-mcp-server:"
                    f"{MCP_SENTRY_VERSION}"
                ),
                # Self-hosted sentry-mcp only speaks stdio; the ToolHive proxy wraps
                # it and fronts it with streamable-http on proxyPort (no mcpPort for
                # stdio).
                "transport": "stdio",
                "proxyPort": 8080,
                "groupRef": {"name": MCP_GROUP_NAME},
                "env": sentry_env,
                "secrets": [
                    {
                        "name": SENTRY_TOKEN_SECRET_NAME,
                        "key": SENTRY_TOKEN_SECRET_KEY,
                        "targetEnvName": "SENTRY_ACCESS_TOKEN",
                    }
                ],
                # Needs outbound access to the Sentry API. Tighten to an allow-list
                # profile (sentry.io + .sentry.io, port 443) once the builtin
                # profile proves out.
                "permissionProfile": {
                    "type": "builtin",
                    "name": "network",
                },
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                },
            },
            opts=ResourceOptions(depends_on=[swe_mcpgroup, sentry_token_secret]),
        )
        servers.append(sentry_mcpserver)

    return ToolhiveSWEMCPServers(
        group=swe_mcpgroup,
        servers=servers,
    )
