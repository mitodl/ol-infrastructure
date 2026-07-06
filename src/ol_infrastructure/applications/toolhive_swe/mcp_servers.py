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
from pulumi import ResourceOptions, StackReference

from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and every backend/virtual server that references it.
MCP_GROUP_NAME = "swe-tools"


class ToolhiveSWEMCPServers(NamedTuple):
    """Handles to the group and backend server CRs for depends_on wiring."""

    group: kubernetes.apiextensions.CustomResource
    servers: list[kubernetes.apiextensions.CustomResource]


def create_mcp_servers(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    cluster_stack: StackReference,
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

    return ToolhiveSWEMCPServers(group=swe_mcpgroup, servers=[fetch_mcpserver])
