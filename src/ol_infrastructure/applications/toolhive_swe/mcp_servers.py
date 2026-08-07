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

from bridge.lib.versions import (
    MCP_CONTEXT7_VERSION,
    MCP_GRAFANA_VERSION,
    MCP_PROXY_FOR_AWS_VERSION,
    MCP_SENTRY_VERSION,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Name shared by the MCPGroup and every backend/virtual server that references it.
MCP_GROUP_NAME = "swe-tools"

# ServiceAccount the aws backend runs under. The OLEKSAuthBinding in __main__.py
# creates it annotated with the IRSA role ARN; the EKS pod-identity webhook turns
# that annotation into AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE on the pod,
# which is all boto3 — and therefore mcp-proxy-for-aws — needs to sign SigV4.
# Deliberately its own SA rather than a generic namespace one: the AWS read grant
# should reach exactly one workload.
AWS_MCP_SERVICE_ACCOUNT_NAME = "toolhive-swe-aws-mcp"

# Regional endpoint of AWS's managed MCP server. us-east-1 is where OL resources
# live; eu-central-1 is the only other endpoint AWS offers.
AWS_MCP_ENDPOINT = "https://aws-mcp.us-east-1.api.aws/mcp"

# K8s Secret holding the Grafana Cloud service account token, materialised from
# encrypted stack config and injected into the grafana MCPServer via ToolHive's
# ``spec.secrets`` -> ``targetEnvName`` mechanism.
GRAFANA_TOKEN_SECRET_NAME = "toolhive-swe-grafana-token"  # noqa: S105  # pragma: allowlist secret
GRAFANA_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret

# K8s Secret holding the Sentry user auth token, materialised from encrypted stack
# config and injected into the sentry MCPServer the same way as the Grafana token.
SENTRY_TOKEN_SECRET_NAME = "toolhive-swe-sentry-token"  # noqa: S105  # pragma: allowlist secret
SENTRY_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret

# K8s Secret holding the Context7 API key, materialised from encrypted stack config
# and injected into the context7 MCPServer the same way as the Grafana token.
CONTEXT7_TOKEN_SECRET_NAME = "toolhive-swe-context7-token"  # noqa: S105  # pragma: allowlist secret
CONTEXT7_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret


class ToolhiveSWEMCPServers(NamedTuple):
    """Handles to the group and backend server CRs for depends_on wiring."""

    group: kubernetes.apiextensions.CustomResource
    servers: list[kubernetes.apiextensions.CustomResource]


def create_mcp_servers(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    cluster_stack: StackReference,
    toolhive_swe_config: Config,
    aws_mcp_service_account: kubernetes.core.v1.ServiceAccount,
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
            #
            # ``--allowed-hosts *`` disables the Host-header (DNS-rebind) check
            # added in mcp-grafana 0.17.1 (upstream PR #957). That check defaults
            # to loopback-only (localhost/127.0.0.1 : 8000), so the ToolHive proxy
            # and vMCP health checks — which reach the workload via cluster-DNS
            # names — get 403 "host not allowed", the vMCP marks grafana
            # unavailable, and every grafana tool drops out of the aggregate.
            # ``*`` is safe here: the workload is a ClusterIP reachable only
            # through the ToolHive proxy (which enforces auth), never exposed
            # externally.
            "args": [
                "--transport",
                "streamable-http",
                "--endpoint-path",
                "/mcp",
                "--allowed-hosts",
                "*",
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

    # Context7 MCP server (https://github.com/upstash/context7) running in its
    # ``stdio`` mode via ToolHive's prebuilt npx wrapper image; the operator's proxy
    # exposes it over streamable-http like the other backends. Provides version-
    # accurate, up-to-date library/framework documentation to reduce hallucinated
    # APIs. Context7 works unauthenticated for basic usage, but an API key raises
    # rate limits and improves performance; every user shares the one key, which is
    # fine here since the key only governs quota, not per-user data access.
    #
    # Gated behind a per-stack boolean so it only runs where explicitly enabled
    # (currently Production only). When disabled neither the token Secret nor the
    # MCPServer is created, so lower stacks don't need a ``context7_api_key``:
    #   pulumi config set toolhive_swe:context7_enabled true
    #   pulumi config set --secret toolhive_swe:context7_api_key -- <key>
    if toolhive_swe_config.get_bool("context7_enabled"):
        context7_token_secret = kubernetes.core.v1.Secret(
            f"toolhive-swe-context7-token-secret-{stack_info.env_suffix}",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=CONTEXT7_TOKEN_SECRET_NAME,
                namespace=namespace,
                labels=k8s_global_labels,
            ),
            type="Opaque",
            string_data={
                CONTEXT7_TOKEN_SECRET_KEY: toolhive_swe_config.require_secret(
                    "context7_api_key"
                ),
            },
            opts=ResourceOptions(),
        )
        context7_mcpserver = kubernetes.apiextensions.CustomResource(
            f"toolhive-swe-context7-mcpserver-{stack_info.env_suffix}",
            api_version="toolhive.stacklok.dev/v1beta1",
            kind="MCPServer",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="context7",
                namespace=namespace,
                labels=k8s_global_labels,
            ),
            spec={
                "image": (
                    f"ghcr.io/stacklok/dockyard/npx/context7:{MCP_CONTEXT7_VERSION}"
                ),
                # context7 only speaks stdio; the ToolHive proxy wraps it and fronts
                # it with streamable-http on proxyPort (no mcpPort for stdio).
                "transport": "stdio",
                "proxyPort": 8080,
                "groupRef": {"name": MCP_GROUP_NAME},
                "secrets": [
                    {
                        "name": CONTEXT7_TOKEN_SECRET_NAME,
                        "key": CONTEXT7_TOKEN_SECRET_KEY,
                        "targetEnvName": "CONTEXT7_API_KEY",
                    }
                ],
                # Needs outbound access to the Context7 API. Tighten to an allow-list
                # profile (context7.com, port 443) once the builtin profile proves
                # out.
                "permissionProfile": {
                    "type": "builtin",
                    "name": "network",
                },
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                },
            },
            opts=ResourceOptions(depends_on=[swe_mcpgroup, context7_token_secret]),
        )
        servers.append(context7_mcpserver)

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

    # AWS backend: the managed AWS MCP Server (https://aws-mcp.us-east-1.api.aws/mcp,
    # GA May 2026) reached through AWS's official `mcp-proxy-for-aws` SigV4 proxy.
    # The proxy is a thin stdio bridge — every tool call executes on the AWS-hosted
    # endpoint, not here.
    #
    # NOT the self-hostable awslabs `aws-api-mcp-server`: AWS has marked that one
    # "entering end of development" and its tool descriptions now emit a deprecation
    # notice to the agent on every call.
    #
    # Read-only is enforced ENTIRELY by the IRSA role's IAM (AWS-managed
    # ReadOnlyAccess plus an explicit Deny on secret-material reads, authored in
    # __main__.py). That matters because the operations account is the same AWS
    # account in CI, QA and Production (no assumeRole separation anywhere in
    # Pulumi.operations.*.yaml), so this role reads Production resources no matter
    # which stack provisions it.
    #
    # The proxy's `--read-only` flag is deliberately NOT set, despite the name.
    # It withholds every tool not annotated readOnlyHint=true, and the only tool
    # that reaches AWS APIs at all — `aws___run_script`, which runs Python against
    # the account — can never carry that annotation, since whether it writes
    # depends on the code it is handed. Verified on the live CI backend: with the
    # flag set the aggregate exposed six tools, all of them documentation/skills
    # lookups, and nothing that could see an S3 bucket. On this server
    # `--read-only` means "no account access", not "read-only account access", so
    # it is mutually exclusive with the reason this backend exists.
    #
    # IAM is the better control anyway: AWS enforces it server-side on every API
    # call, rather than by filtering a tool list the agent is offered. The write
    # tools it re-admits are inert without permissions — `run_script` can call
    # anything but only reads succeed, and `get_presigned_url` can only mint URLs
    # for operations the role could already perform (ReadOnlyAccess grants no
    # PutObject, and the Deny covers GetObject).
    #
    # Unlike every other backend here there is no token Secret: credentials come
    # from IRSA via the boto3 default chain. Gated per-stack like sentry/context7:
    #   pulumi config set toolhive_swe:aws_mcp_enabled true
    if toolhive_swe_config.get_bool("aws_mcp_enabled"):
        aws_mcpserver = kubernetes.apiextensions.CustomResource(
            f"toolhive-swe-aws-mcpserver-{stack_info.env_suffix}",
            api_version="toolhive.stacklok.dev/v1beta1",
            kind="MCPServer",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="aws",
                namespace=namespace,
                labels=k8s_global_labels,
            ),
            spec={
                "image": (
                    "public.ecr.aws/mcp-proxy-for-aws/mcp-proxy-for-aws:"
                    f"{MCP_PROXY_FOR_AWS_VERSION}"
                ),
                # The proxy only speaks stdio; the ToolHive proxy wraps it and
                # fronts it with streamable-http on proxyPort (no mcpPort for
                # stdio), the same shape as the sentry and context7 backends.
                "transport": "stdio",
                "proxyPort": 8080,
                "groupRef": {"name": MCP_GROUP_NAME},
                # Appended to the image's `mcp-proxy-for-aws` ENTRYPOINT. The
                # endpoint URL is POSITIONAL and must come first.
                #
                # `--metadata AWS_REGION` sets the default region for the AWS
                # operations the managed server performs. Unset it defaults to
                # us-east-1 anyway; stating it makes the default ours.
                "args": [
                    AWS_MCP_ENDPOINT,
                    "--metadata",
                    "AWS_REGION=us-east-1",
                    "--disable-telemetry",
                ],
                # IRSA: this sets the SA on the MCP workload pod (the `mcp`
                # container), which is where boto3 runs and signs SigV4.
                "serviceAccount": AWS_MCP_SERVICE_ACCOUNT_NAME,
                # Region for SigV4 signing and the STS call. The pod-identity
                # webhook injects a region too, but setting it here makes the
                # value ours rather than inherited.
                "env": [{"name": "AWS_REGION", "value": "us-east-1"}],
                # Needs outbound access to aws-mcp.us-east-1.api.aws AND to
                # sts.us-east-1.amazonaws.com (AssumeRoleWithWebIdentity for
                # IRSA). Tighten to an allow-list profile (.api.aws +
                # .amazonaws.com, port 443) once the builtin profile proves out.
                "permissionProfile": {
                    "type": "builtin",
                    "name": "network",
                },
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                },
            },
            # The ServiceAccount stands in for the token Secret the other
            # backends wait on: without it the operator reconciles this into a
            # pod that has no way to obtain credentials.
            opts=ResourceOptions(depends_on=[swe_mcpgroup, aws_mcp_service_account]),
        )
        servers.append(aws_mcpserver)

    return ToolhiveSWEMCPServers(
        group=swe_mcpgroup,
        servers=servers,
    )
