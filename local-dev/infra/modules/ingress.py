"""cert-manager and APISIX ingress resources for the local-dev infra stack.

APISIX's chart values, its GatewayClass and Gateway, and the
identity-header-strip global rule come from the code the deployed clusters use
(src/ol_infrastructure/infrastructure/aws/eks/apisix_definition.py). This
module only supplies what differs on k3d.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from ol_infrastructure.infrastructure.aws.eks.apisix_definition import (
    APISIX_CHART_REPOSITORY,
    APISIX_NAMESPACE,
    APISIX_RELEASE_NAME,
    GATEWAY_ERROR_PAGES_CONFIGMAP_NAME,
    GATEWAY_NAME,
    IDENTITY_HEADER_STRIP_RULE_NAME,
    ApisixGatewayConfig,
    ApisixGatewayService,
    ApisixTrustedCA,
    apisix_helm_values,
    gateway_class_spec,
    gateway_error_pages,
    gateway_spec,
    identity_header_strip_rule_spec,
)

from .tls import APISIX_TRUSTED_CA_FILENAME

# The environment suffix local-dev's routes use, e.g. for the MIT Learn
# session cookie name.
LOCAL_ENV_SUFFIX = "dev"
# k3d publishes these on the host's ports 80 and 443
# (local-dev/cluster/k3d-config.yaml).
HTTP_NODE_PORT = 30080
TLS_NODE_PORT = 30443


@dataclass
class IngressResources:
    cert_manager: k8s.helm.v3.Release
    apisix: k8s.helm.v3.Release


def create_ingress(  # noqa: PLR0913
    _k8s: Callable[..., ResourceOptions],
    namespaces: dict[str, k8s.core.v1.Namespace],
    apisix_admin_key: Output,
    apisix_viewer_key: Output,
    cert_manager_version: str,
    apisix_version: str,
    trusted_ca_secret: k8s.core.v1.Secret,
    trusted_ca_checksum: str,
) -> IngressResources:
    """Deploy cert-manager and APISIX into the operations namespace."""
    operations_ns = namespaces["operations"]

    cert_manager = k8s.helm.v3.Release(
        "cert-manager",
        k8s.helm.v3.ReleaseArgs(
            name="cert-manager",
            chart="cert-manager",
            version=cert_manager_version,
            namespace="operations",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://charts.jetstack.io",
            ),
            cleanup_on_fail=True,
            values={
                "crds": {"enabled": True, "keep": True},
                "replicaCount": 1,
                "enableCertificateOwnerRef": True,
                "prometheus": {"enabled": False},
            },
        ),
        opts=_k8s(parent=operations_ns),
    )

    error_pages = k8s.core.v1.ConfigMap(
        "apisix-error-pages",
        metadata={
            "name": GATEWAY_ERROR_PAGES_CONFIGMAP_NAME,
            "namespace": APISIX_NAMESPACE,
        },
        data=gateway_error_pages(),
        opts=_k8s(parent=operations_ns),
    )

    gateway_config = ApisixGatewayConfig(
        env_suffix=LOCAL_ENV_SUFFIX,
        admin_key=apisix_admin_key,
        viewer_key=apisix_viewer_key,
        service=ApisixGatewayService(
            type="NodePort",
            http_node_port=HTTP_NODE_PORT,
            tls_node_port=TLS_NODE_PORT,
        ),
        # 512Mi OOM-kills APISIX under active OIDC load (JWKS cache +
        # sessions), crash-looping the gateway and dropping all app auth.
        # The deployed default is double the 400Mi request, 800Mi.
        memory_limit="1Gi",
        # Lets openid-connect verify Keycloak's mkcert certificate with the
        # deployed default ssl_verify=true (see tls.py).
        trusted_ca=ApisixTrustedCA(
            secret_name=trusted_ca_secret.metadata.name,
            filename=APISIX_TRUSTED_CA_FILENAME,
        ),
        # APISIX reads the trusted CA file only at startup, through a subPath
        # mount, so a new bundle has to roll the pod.
        pod_annotations={"checksum/trusted-ca": trusted_ca_checksum},
    )

    # Helm installs the chart's CRDs on first install, including the Gateway
    # API CRDs the ingress controller subchart bundles. The deployed clusters
    # skip those and install the Gateway API bundle at GATEWAY_API_VERSION
    # themselves (setup_traefik).
    apisix = k8s.helm.v3.Release(
        "apisix",
        k8s.helm.v3.ReleaseArgs(
            name=APISIX_RELEASE_NAME,
            chart="apisix",
            version=apisix_version,
            namespace=APISIX_NAMESPACE,
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo=APISIX_CHART_REPOSITORY,
            ),
            cleanup_on_fail=True,
            timeout=600,
            values=apisix_helm_values(gateway_config),
        ),
        opts=_k8s(parent=operations_ns, depends_on=[error_pages]),
    )

    gateway_class = k8s.apiextensions.CustomResource(
        "apisix-gateway-class",
        api_version="gateway.networking.k8s.io/v1",
        kind="GatewayClass",
        metadata={"name": GATEWAY_NAME},
        spec=gateway_class_spec(),
        opts=_k8s(parent=operations_ns, depends_on=[apisix]),
    )

    k8s.apiextensions.CustomResource(
        "apisix-gateway",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata={"name": GATEWAY_NAME, "namespace": APISIX_NAMESPACE},
        spec=gateway_spec(),
        opts=_k8s(parent=operations_ns, depends_on=[gateway_class]),
    )

    k8s.apiextensions.CustomResource(
        "apisix-identity-header-strip-global-rule",
        api_version="apisix.apache.org/v2",
        kind="ApisixGlobalRule",
        metadata={
            "name": IDENTITY_HEADER_STRIP_RULE_NAME,
            "namespace": APISIX_NAMESPACE,
        },
        spec=identity_header_strip_rule_spec(),
        opts=_k8s(parent=operations_ns, depends_on=[apisix]),
    )

    return IngressResources(cert_manager=cert_manager, apisix=apisix)
