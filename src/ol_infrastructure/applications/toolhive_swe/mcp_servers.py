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

from bridge.lib.versions import MCP_GRAFANA_VERSION
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and every backend/virtual server that references it.
MCP_GROUP_NAME = "swe-tools"

# K8s Secret holding the Grafana Cloud service account token, materialised from
# encrypted stack config and injected into the grafana MCPServer via ToolHive's
# ``spec.secrets`` -> ``targetEnvName`` mechanism.
GRAFANA_TOKEN_SECRET_NAME = "toolhive-swe-grafana-token"  # noqa: S105  # pragma: allowlist secret
GRAFANA_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret


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

    return ToolhiveSWEMCPServers(
        group=swe_mcpgroup,
        servers=[fetch_mcpserver, grafana_mcpserver],
    )
