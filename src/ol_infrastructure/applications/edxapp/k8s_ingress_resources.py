# ruff: noqa: PLR0913
import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import Config

from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_k8s_ingress_resources(
    edxapp_config: Config,
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    lms_webapp_deployment_name: str,
    cms_webapp_deployment_name: str,
    lms_webapp_deployment: kubernetes.apps.v1.Deployment,
    cms_webapp_deployment: kubernetes.apps.v1.Deployment,
):
    """Create ingress resources for edxapp.

    :param edxapp_config: The Pulumi config object for the edxapp application.
    :param stack_info: The stack info object.
    :param namespace: The Kubernetes namespace to deploy into.
    :param k8s_global_labels: A dictionary of global labels to apply to the resources.
    :param lms_webapp_deployment_name: The name of the LMS webapp deployment.
    :param cms_webapp_deployment_name: The name of the CMS webapp deployment.
    :param lms_webapp_deployment: The LMS webapp deployment resource.
    :param cms_webapp_deployment: The CMS webapp deployment resource.
    """
    backend_lms_domain = edxapp_config.require("backend_lms_domain")
    backend_studio_domain = edxapp_config.require("backend_studio_domain")
    backend_preview_domain = edxapp_config.require("backend_preview_domain")

    frontend_lms_domain = edxapp_config.require_object("domains")["lms"]
    frontend_studio_domain = edxapp_config.require_object("domains")["studio"]
    frontend_preview_domain = edxapp_config.require_object("domains")["preview"]

    gateway_config = OLEKSGatewayConfig(
        gateway_name=f"{stack_info.env_prefix}-edxapp",
        namespace=namespace,
        labels=k8s_global_labels,
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https-backend-lms",
                hostname=backend_lms_domain,
                port=8443,
                protocol="HTTPS",
                tls_mode="Terminate",
                certificate_secret_name=f"{stack_info.env_prefix}-lms-tls",
                certificate_secret_namespace=namespace,
            ),
            OLEKSGatewayListenerConfig(
                name="https-backend-studio",
                hostname=backend_studio_domain,
                port=8443,
                protocol="HTTPS",
                tls_mode="Terminate",
                certificate_secret_name=f"{stack_info.env_prefix}-studio-tls",
                certificate_secret_namespace=namespace,
            ),
            OLEKSGatewayListenerConfig(
                name="https-backend-preview",
                hostname=backend_preview_domain,
                port=8443,
                protocol="HTTPS",
                tls_mode="Terminate",
                certificate_secret_name=f"{stack_info.env_prefix}-preview-tls",
                certificate_secret_namespace=namespace,
            ),
        ],
        routes=[
            OLEKSGatewayRouteConfig(
                name="backend-lms-route",
                listener_name="https-backend-lms",
                hostnames=[backend_lms_domain, frontend_lms_domain],
                port=8443,
                backend_service_name=lms_webapp_deployment_name,
                backend_service_namespace=namespace,
                backend_service_port=8000,
                matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
            ),
            OLEKSGatewayRouteConfig(
                name="backend-studio-route",
                listener_name="https-backend-studio",
                hostnames=[backend_studio_domain, frontend_studio_domain],
                port=8443,
                backend_service_name=cms_webapp_deployment_name,
                backend_service_namespace=namespace,
                backend_service_port=8000,
                matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
            ),
            OLEKSGatewayRouteConfig(
                name="backend-preview-route",
                listener_name="https-backend-preview",
                hostnames=[backend_preview_domain, frontend_preview_domain],
                port=8443,
                backend_service_name=lms_webapp_deployment_name,
                backend_service_namespace=namespace,
                backend_service_port=8000,
                matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
            ),
        ],
    )

    OLEKSGateway(
        f"ol-{stack_info.env_prefix}-edxapp-gateway-{stack_info.env_suffix}",
        gateway_config=gateway_config,
        opts=pulumi.ResourceOptions(
            depends_on=[
                lms_webapp_deployment,
                cms_webapp_deployment,
                # host_rewrite_middleware,
            ]
        ),
    )
