"""TLS secrets and CA ConfigMaps for the local-dev infra stack.

mkcert generates a wildcard certificate during setup.sh.  This module copies
the cert+key into every namespace that needs TLS termination, and distributes
the mkcert root CA so containers can trust local HTTPS endpoints (e.g. Keycloak).
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from .helpers import read_file_b64


@dataclass
class TlsResources:
    tls_secret: k8s.core.v1.Secret
    """TLS secret in the local-infra namespace."""
    tls_secret_ops: k8s.core.v1.Secret
    """TLS secret in the operations namespace."""
    app_tls_secrets: dict[str, k8s.core.v1.Secret]
    """Per-app-namespace TLS secrets keyed by namespace name."""
    app_ca_configmaps: dict[str, k8s.core.v1.ConfigMap]
    """Per-app-namespace mkcert CA ConfigMaps keyed by namespace name."""


def create_tls_resources(
    _k8s: Callable[..., ResourceOptions],
    namespaces: dict[str, k8s.core.v1.Namespace],
    cert_path: Path,
    key_path: Path,
    ca_cert_path: Path,
) -> TlsResources:
    """Create TLS secrets and CA ConfigMaps in all required namespaces."""
    tls_cert_b64 = read_file_b64(cert_path)
    tls_key_b64 = read_file_b64(key_path)

    try:
        ca_cert_content = ca_cert_path.read_text()
    except FileNotFoundError as e:
        msg = (
            f"mkcert root CA not found: {ca_cert_path}\n"
            "Run ./local-dev/scripts/setup.sh to generate local TLS certificates."
        )
        raise SystemExit(msg) from e

    def _tls_secret(
        resource_name: str, namespace: str, parent: k8s.core.v1.Namespace
    ) -> k8s.core.v1.Secret:
        return k8s.core.v1.Secret(
            resource_name,
            metadata={"name": "local-dev-tls", "namespace": namespace},
            type="kubernetes.io/tls",
            data={"tls.crt": tls_cert_b64, "tls.key": tls_key_b64},
            opts=_k8s(parent=parent),
        )

    def _ca_configmap(
        resource_name: str, namespace: str, parent: k8s.core.v1.Namespace
    ) -> k8s.core.v1.ConfigMap:
        return k8s.core.v1.ConfigMap(
            resource_name,
            metadata={"name": "mkcert-root-ca", "namespace": namespace},
            data={"rootCA.pem": ca_cert_content},
            opts=_k8s(parent=parent),
        )

    tls_secret = _tls_secret("local-dev-tls", "local-infra", namespaces["local-infra"])
    tls_secret_ops = _tls_secret(
        "local-dev-tls-operations", "operations", namespaces["operations"]
    )

    app_namespaces = ("mit-learn", "learn-ai", "mitxonline", "odl-video-service")
    app_tls_secrets = {
        ns: _tls_secret(f"local-dev-tls-{ns}", ns, namespaces[ns])
        for ns in app_namespaces
    }
    app_ca_configmaps = {
        ns: _ca_configmap(f"mkcert-root-ca-{ns}", ns, namespaces[ns])
        for ns in app_namespaces
    }

    return TlsResources(
        tls_secret=tls_secret,
        tls_secret_ops=tls_secret_ops,
        app_tls_secrets=app_tls_secrets,
        app_ca_configmaps=app_ca_configmaps,
    )
