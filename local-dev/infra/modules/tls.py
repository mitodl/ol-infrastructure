"""TLS secrets and CA ConfigMaps for the local-dev infra stack.

mkcert generates a wildcard certificate during setup.sh.  This module copies
the cert+key into every namespace that needs TLS termination, and distributes
the mkcert root CA so containers can trust local HTTPS endpoints (e.g. Keycloak).
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import certifi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from .helpers import read_file_b64
from .namespaces import app_namespaces_for


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
    app_ca_bundle_configmaps: dict[str, k8s.core.v1.ConfigMap]
    """Per-app-namespace ``CA_BUNDLE_CONFIGMAP`` ConfigMaps keyed by namespace
    name."""
    apisix_trusted_ca: k8s.core.v1.Secret
    """CA bundle APISIX verifies upstream TLS against, in the operations
    namespace; its file is ``APISIX_TRUSTED_CA_FILENAME``."""
    apisix_trusted_ca_checksum: str
    """SHA-256 of that bundle, for a pod annotation that rolls APISIX when it
    changes."""


APISIX_TRUSTED_CA_FILENAME = "ca-bundle.pem"
# Public roots plus the mkcert root, in every app namespace, for pods that make
# HTTPS calls to local hostnames. Mounted as a directory: SSL_CERT_FILE and
# REQUESTS_CA_BUNDLE name the file in it, and Concourse copies the whole
# directory into resource containers as /etc/ssl/certs, which is why the file
# has the name Debian and Alpine images read there.
CA_BUNDLE_CONFIGMAP = "local-dev-ca-bundle"
CA_BUNDLE_FILENAME = "ca-certificates.crt"
# The same bundle on the host, for a Pulumi program's own HTTPS calls to a local
# hostname — REQUESTS_CA_BUNDLE points the apps-infra stack at it so the SCIM
# remote-provider resource can reach Keycloak. Written by
# local-dev/scripts/write-ca-bundle.sh before the stack runs, not by the stack:
# `pulumi up --refresh` does its read() pass before the program, so a program
# that wrote the bundle would write it after the first call that needs it.
HOST_CA_BUNDLE_FILENAME = "combined-ca.pem"


def read_mkcert_root(ca_cert_path: Path) -> str:
    """Read the mkcert root CA, or explain how to generate it."""
    try:
        return ca_cert_path.read_text()
    except FileNotFoundError as e:
        msg = (
            f"mkcert root CA not found: {ca_cert_path}\n"
            "Run ./local-dev/scripts/setup.sh to generate local TLS certificates."
        )
        raise SystemExit(msg) from e


def trusted_ca_bundle(ca_cert_content: str) -> str:
    """Public roots plus the mkcert root.

    Whoever reads this reads it *instead of* their own trust store, so the public
    roots have to be in it. certifi comes from the venv the Pulumi programs run
    under, which keeps the bundle's public roots and theirs the same.
    """
    return Path(certifi.where()).read_text() + "\n" + ca_cert_content


def write_host_ca_bundle(ca_cert_path: Path) -> Path:
    """Write the trust bundle next to the mkcert root and return its path.

    Rewritten on every call rather than generated once by setup.sh: a checkout
    whose certs predate the bundle then heals itself instead of failing with a
    TLS error that names no cause, and a bundle kept from an older certifi
    cannot silently pin a stale root set.
    """
    bundle_path = ca_cert_path.parent / HOST_CA_BUNDLE_FILENAME
    bundle_path.write_text(trusted_ca_bundle(read_mkcert_root(ca_cert_path)))
    return bundle_path


def create_tls_resources(
    _k8s: Callable[..., ResourceOptions],
    namespaces: dict[str, k8s.core.v1.Namespace],
    cert_path: Path,
    key_path: Path,
    ca_cert_path: Path,
    enabled_apps: Iterable[str] = (),
) -> TlsResources:
    """Create TLS secrets and CA ConfigMaps in all required namespaces."""
    tls_cert_b64 = read_file_b64(cert_path)
    tls_key_b64 = read_file_b64(key_path)

    ca_cert_content = read_mkcert_root(ca_cert_path)

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

    app_namespaces = app_namespaces_for(enabled_apps)
    app_tls_secrets = {
        ns: _tls_secret(f"local-dev-tls-{ns}", ns, namespaces[ns])
        for ns in app_namespaces
    }
    app_ca_configmaps = {
        ns: _ca_configmap(f"mkcert-root-ca-{ns}", ns, namespaces[ns])
        for ns in app_namespaces
    }

    # For APISIX the mkcert root is what lets openid-connect verify Keycloak:
    # the discovery document's token and JWKS endpoints are on sso.ol.<domain>,
    # which the CoreDNS override sends back through APISIX's own mkcert
    # certificate.
    ca_bundle = trusted_ca_bundle(ca_cert_content)
    app_ca_bundle_configmaps = {
        ns: k8s.core.v1.ConfigMap(
            f"{CA_BUNDLE_CONFIGMAP}-{ns}",
            metadata={"name": CA_BUNDLE_CONFIGMAP, "namespace": ns},
            data={CA_BUNDLE_FILENAME: ca_bundle},
            opts=_k8s(parent=namespaces[ns]),
        )
        for ns in app_namespaces
    }
    apisix_trusted_ca = k8s.core.v1.Secret(
        "apisix-trusted-ca",
        metadata={"name": "apisix-trusted-ca", "namespace": "operations"},
        string_data={APISIX_TRUSTED_CA_FILENAME: ca_bundle},
        opts=_k8s(parent=namespaces["operations"]),
    )

    return TlsResources(
        tls_secret=tls_secret,
        tls_secret_ops=tls_secret_ops,
        app_tls_secrets=app_tls_secrets,
        app_ca_configmaps=app_ca_configmaps,
        app_ca_bundle_configmaps=app_ca_bundle_configmaps,
        apisix_trusted_ca=apisix_trusted_ca,
        apisix_trusted_ca_checksum=hashlib.sha256(ca_bundle.encode()).hexdigest(),
    )
