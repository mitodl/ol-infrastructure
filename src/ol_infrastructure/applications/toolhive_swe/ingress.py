"""Internet exposure for the swe vMCP through the shared APISIX gateway.

The ``VirtualMCPServer`` is exposed to the internet on the operations cluster
using the hybrid HTTPRoute + ApisixTls pattern (ADR-0003): cert-manager issues a
Let's Encrypt certificate for the host and the paired ApisixTls resource binds
it to the APISIX gateway, while a Gateway API HTTPRoute routes every path
(``/mcp``, ``/authorize``, ``/token``, ``/oauth/callback``, ``/.well-known/*``)
to the vMCP Service. APISIX does NOT participate in authentication — it only
terminates TLS and proxies through; all auth (OAuth endpoints + token
validation) happens inside the vMCP, so no plugins are attached.

The hostname must also be present in the operations EKS stack's
``eks:apisix_domains`` so external-dns points it at the APISIX NLB.
"""

from pulumi import Resource, ResourceOptions

from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# The VirtualMCPServer's Service, created by the operator when it reconciles the
# ``swe-vmcp`` CR. It listens on port 4483 (not the backend proxy's 8080).
VMCP_SERVICE_NAME = "vmcp-swe-vmcp"
VMCP_SERVICE_PORT = 4483
VMCP_TLS_SECRET_NAME = "toolhive-swe-vmcp-tls"  # noqa: S105  # pragma: allowlist secret


def create_ingress_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    vmcp_domain: str,
    swe_virtualmcpserver: Resource,
) -> tuple[OLCertManagerCert, OLApisixHTTPRoute]:
    """Provision the cert-manager Certificate/ApisixTls and the APISIX HTTPRoute."""
    # Per-host TLS: cert-manager issues a Let's Encrypt cert into
    # VMCP_TLS_SECRET_NAME and the paired ApisixTls resource binds it to the host
    # on the APISIX gateway.
    vmcp_cert = OLCertManagerCert(
        f"toolhive-swe-vmcp-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="toolhive-swe-vmcp",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=VMCP_TLS_SECRET_NAME,
            dns_names=[vmcp_domain],
        ),
        opts=ResourceOptions(depends_on=[swe_virtualmcpserver]),
    )

    # Gateway API HTTPRoute attaching the host to the shared operations APISIX
    # gateway and routing all paths (MCP + OAuth + /.well-known/*) to the vMCP
    # Service. No plugins: authentication happens inside the vMCP.
    vmcp_httproute = OLApisixHTTPRoute(
        f"toolhive-swe-vmcp-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="vmcp",
                hosts=[vmcp_domain],
                paths=["/*"],
                backend_service_name=VMCP_SERVICE_NAME,
                # Numeric port: the component maps the name "http" to 8071, which
                # is wrong for this Service — pass the real port explicitly.
                backend_service_port=VMCP_SERVICE_PORT,
                plugins=[],
            ),
        ],
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        opts=ResourceOptions(depends_on=[swe_virtualmcpserver]),
    )

    return vmcp_cert, vmcp_httproute
