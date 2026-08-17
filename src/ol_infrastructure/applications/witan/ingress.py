"""Internet exposure for witan through the shared APISIX gateway.

Same pattern as ``toolhive_swe/ingress.py`` (ADR-0003 hybrid HTTPRoute +
ApisixTls): cert-manager issues a Let's Encrypt certificate for the host and
the paired ApisixTls resource binds it to the APISIX gateway, while a Gateway
API HTTPRoute routes every path to witan's Service.

APISIX does not participate in authentication. It did not before either — the
vMCP validated the forwarded Keycloak JWT and then passed it through to witan,
which validated it again. Now witan's own ``JWTVerifier`` (agent-kit ADR-0004
D1) is the single check, which is what it always effectively was.

★ THE CLIENT-FACING URL IS UNCHANGED BY THE TOOLHIVE REMOVAL, and that is not
a coincidence to leave unstated: clients call
``https://witan.<env>.ol.mit.edu/mcp``, the vMCP served ``/mcp`` on 4483, and
witan's own FastMCP serves ``/mcp`` on 8000 (``WITAN_MCP_PATH``, matching
agent-kit's ``witan serve --path`` default). So every configured client keeps
working with no edit. If witan's default path ever moves, they all break at
once and this is the file that has to change with it.

The hostname must also be present in the operations EKS stack's
``eks:apisix_domains`` so external-dns points it at the APISIX NLB.
"""

from pulumi import Resource, ResourceOptions

from ol_infrastructure.applications.witan.deployment import (
    WITAN_PORT,
    WITAN_SERVICE_NAME,
)
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

WITAN_TLS_SECRET_NAME = "witan-tls"  # noqa: S105  # pragma: allowlist secret


def create_ingress_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_domain: str,
    witan_service: Resource,
) -> tuple[OLCertManagerCert, OLApisixHTTPRoute]:
    """Provision the cert-manager Certificate/ApisixTls and the APISIX HTTPRoute."""
    witan_cert = OLCertManagerCert(
        f"witan-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="witan",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=WITAN_TLS_SECRET_NAME,
            dns_names=[witan_domain],
        ),
        opts=ResourceOptions(depends_on=[witan_service]),
    )

    # Still `/*` rather than narrowing to `/mcp`, deliberately: this change
    # already replaces the entire serving tier, and altering the external path
    # surface in the same step would make a rollback ambiguous about which half
    # broke. One variable at a time.
    #
    # The visible consequence is that `/health` becomes publicly reachable and
    # reports witan's version. It carries no graph data and no per-actor state,
    # and the kubelet reaches it via the pod IP rather than through here, so
    # narrowing this to `/mcp` later costs nothing operationally — a follow-up
    # worth taking once this cutover has settled.
    witan_httproute = OLApisixHTTPRoute(
        f"witan-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="witan",
                hosts=[witan_domain],
                paths=["/*"],
                backend_service_name=WITAN_SERVICE_NAME,
                backend_service_port=WITAN_PORT,
                plugins=[],
            ),
        ],
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        opts=ResourceOptions(depends_on=[witan_service]),
    )

    return witan_cert, witan_httproute
