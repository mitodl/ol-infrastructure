"""Internet exposure for the witan vMCP through the shared APISIX gateway.

Identical pattern to ``toolhive_swe/ingress.py`` (ADR-0003 hybrid HTTPRoute +
ApisixTls): cert-manager issues a Let's Encrypt certificate for the host and
the paired ApisixTls resource binds it to the APISIX gateway, while a Gateway
API HTTPRoute routes every path to the vMCP Service. APISIX does not
participate in authentication here either — the External-OIDC-provider vMCP
validates the forwarded Keycloak JWT itself (see ``__main__.py``).

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

# The VirtualMCPServer's Service, created by the operator when it reconciles
# the ``witan-vmcp`` CR (name convention: ``vmcp-<vmcp-resource-name>``,
# confirmed against toolhive_swe's ``vmcp-swe-vmcp``). Port 4483, same as
# every other vMCP on this operator — not the backend proxy's 8080.
VMCP_SERVICE_NAME = "vmcp-witan-vmcp"
VMCP_SERVICE_PORT = 4483
VMCP_TLS_SECRET_NAME = "witan-vmcp-tls"  # noqa: S105  # pragma: allowlist secret


def create_ingress_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    vmcp_domain: str,
    witan_virtualmcpserver: Resource,
) -> tuple[OLCertManagerCert, OLApisixHTTPRoute]:
    """Provision the cert-manager Certificate/ApisixTls and the APISIX HTTPRoute."""
    vmcp_cert = OLCertManagerCert(
        f"witan-vmcp-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="witan-vmcp",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=VMCP_TLS_SECRET_NAME,
            dns_names=[vmcp_domain],
        ),
        opts=ResourceOptions(depends_on=[witan_virtualmcpserver]),
    )

    vmcp_httproute = OLApisixHTTPRoute(
        f"witan-vmcp-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="vmcp",
                hosts=[vmcp_domain],
                paths=["/*"],
                backend_service_name=VMCP_SERVICE_NAME,
                backend_service_port=VMCP_SERVICE_PORT,
                plugins=[],
            ),
        ],
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        opts=ResourceOptions(depends_on=[witan_virtualmcpserver]),
    )

    return vmcp_cert, vmcp_httproute
