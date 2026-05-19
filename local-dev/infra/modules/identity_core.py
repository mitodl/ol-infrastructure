"""Keycloak operator and instance for core infrastructure.

Provisions:
  - Keycloak Operator (official keycloak-k8s-resources manifests)
  - Keycloak CustomResource (CR) instance backed by the shared CNPG cluster
  - APISIX routing (ApisixRoute + ApisixTls) for the Keycloak hostname

The olapps realm is created separately in the apps-infra stack.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
import requests
import yaml as pyyaml
from pulumi import ResourceOptions


@dataclass
class IdentityCoreResources:
    """Resources created by the identity-core module."""

    operator_crds: k8s.yaml.v2.ConfigGroup
    operator: k8s.yaml.v2.ConfigGroup
    instance: k8s.apiextensions.CustomResource
    admin_secret: k8s.core.v1.Secret


def create_identity_core(  # noqa: PLR0913
    _k8s: Callable[..., ResourceOptions],
    k8s_provider: k8s.Provider,  # noqa: ARG001
    local_infra_ns: k8s.core.v1.Namespace,
    apisix_release: k8s.helm.v3.Release,
    tls_secret: k8s.core.v1.Secret,
    keycloak_operator_version: str,
    keycloak_hostname: str,
    keycloak_url: str,  # noqa: ARG001
    root_domain: str,  # noqa: ARG001
    db_cluster: k8s.apiextensions.CustomResource,
) -> IdentityCoreResources:
    """Deploy Keycloak operator, instance, and routes (no realm)."""
    kc_base = (
        "https://raw.githubusercontent.com/keycloak/"
        f"keycloak-k8s-resources/{keycloak_operator_version}/kubernetes"
    )

    # CRDs first — cluster-scoped, no namespace patching needed.
    operator_crds = k8s.yaml.v2.ConfigGroup(
        "keycloak-operator-crds",
        files=[
            f"{kc_base}/keycloaks.k8s.keycloak.org-v1.yml",
            f"{kc_base}/keycloakrealmimports.k8s.keycloak.org-v1.yml",
        ],
        opts=_k8s(parent=local_infra_ns, delete_before_replace=False),
    )

    # Fetch the operator deployment manifest and patch namespace to local-infra
    # before passing to ConfigGroup (yaml.v2 dropped transformations support).
    resp = requests.get(f"{kc_base}/kubernetes.yml", timeout=30)
    resp.raise_for_status()
    kc_resources = [doc for doc in pyyaml.safe_load_all(resp.text) if doc is not None]
    for doc in kc_resources:
        if doc.get("metadata") and doc["kind"] not in (
            "ClusterRole",
            "ClusterRoleBinding",
            "CustomResourceDefinition",
        ):
            doc["metadata"]["namespace"] = "local-infra"

    operator = k8s.yaml.v2.ConfigGroup(
        "keycloak-operator",
        objs=kc_resources,
        opts=_k8s(
            parent=local_infra_ns,
            delete_before_replace=False,
            depends_on=[operator_crds],
        ),
    )

    admin_secret = k8s.core.v1.Secret(
        "keycloak-admin-secret",
        metadata={
            "name": "keycloak-admin-credentials",
            "namespace": "local-infra",
        },
        string_data={
            "username": "admin",
            "password": "admin",  # pragma: allowlist secret
        },
        opts=_k8s(parent=local_infra_ns),
    )

    # Note: instance explicitly depends on the database cluster being ready.
    # The pg-app-credentials secret is created in the core stack (database.py),
    # so we need to ensure the cluster is ready before attempting to create
    # the Keycloak instance.
    instance = k8s.apiextensions.CustomResource(
        "keycloak-instance",
        api_version="k8s.keycloak.org/v2alpha1",
        kind="Keycloak",
        metadata={
            "name": "keycloak",
            "namespace": "local-infra",
        },
        spec={
            "instances": 1,
            "image": (
                "mitodl/keycloak"
                "@sha256:4475afe3c385da6bd240a4a2811fa1231dd3365497ca78c017327c7c4e0ea1e2"
            ),
            # The image is pre-built (kc.sh build was run during Docker build).
            # startOptimized=True uses that build; False would re-run kc.sh build
            # on every pod start (slow). The image already has organization baked
            # in, so we do NOT set features.enabled here — that would cause a
            # build-time options mismatch and crash with --optimized.
            "startOptimized": True,
            "hostname": {
                "hostname": f"https://{keycloak_hostname}",
            },
            "http": {
                "httpEnabled": True,
                "tlsSecret": "local-dev-tls",  # pragma: allowlist secret
            },
            # KC 26: use proxy.headers instead of the deprecated KC_PROXY env var.
            "proxy": {"headers": "xforwarded"},
            "db": {
                "vendor": "postgres",
                "host": "local-pg-rw.local-infra.svc.cluster.local",
                "port": 5432,
                "database": "keycloak",
                "usernameSecret": {
                    "name": "pg-app-credentials",
                    "key": "username",
                },
                "passwordSecret": {
                    "name": "pg-app-credentials",
                    "key": "password",
                },
            },
            "bootstrapAdmin": {
                "user": {
                    "secret": "keycloak-admin-credentials"  # pragma: allowlist secret
                },
            },
            "ingress": {"enabled": False},  # Routed through APISIX.
            "additionalOptions": [
                # Use local cache instead of Infinispan for single-instance
                # deployments. Infinispan is designed for clustered HA setups
                # and requires significant memory for distributed state.
                # Local cache disables clustering and reduces memory footprint
                # by ~40-50%, from ~1700Mi requests to ~800-1000Mi.
                # Appropriate for local development and testing only.
                {"name": "cache", "value": "local"},
                # Reduce embedded cache entry limits to match local dev
                # workload sizes. Default values are sized for production
                # with many concurrent users. Local dev typically has fewer
                # entries, reducing pre-allocation waste.
                {"name": "cache-embedded-keys-max-count", "value": "1000"},
                {
                    "name": "cache-embedded-client-sessions-max-count",
                    "value": "1000",
                },
                {
                    "name": "cache-embedded-authorization-max-count",
                    "value": "1000",
                },
                # Explicitly select the OL freemarker login SPI. The image
                # ships two custom implementations; without this the
                # jcputney freemarker override is used by default and
                # causes an NPE in KC 26.5.
                {"name": "spi-login--provider", "value": "ol-freemarker"},
                # Prevent Infinispan sticky-session cookies from destabilising
                # single-instance deployments.
                {
                    "name": (
                        "spi-sticky-session-encoder-infinispan-should-attach-route"
                    ),
                    "value": "false",
                },
                {
                    "name": "spi-email-smtp-host",
                    "value": "mailpit.local-infra.svc.cluster.local",
                },
                {"name": "spi-email-smtp-port", "value": "1025"},
                {"name": "spi-email-smtp-auth", "value": "false"},
                {"name": "spi-email-smtp-ssl", "value": "false"},
                {"name": "spi-email-smtp-starttls", "value": "false"},
            ],
            "resources": {
                "limits": {"memory": "2Gi"},
            },
            "unsupported": {
                "podTemplate": {
                    "spec": {
                        "containers": [
                            {
                                "env": [
                                    # Image has OTel compiled in at build time.
                                    # Without a receiver, SDK init timeouts
                                    # destabilise the pod.
                                    {
                                        "name": "OTEL_SDK_DISABLED",
                                        "value": "true",
                                    },
                                    {
                                        "name": "KC_HOSTNAME_STRICT",
                                        "value": "false",
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
        },
        opts=_k8s(
            parent=local_infra_ns,
            depends_on=[operator, admin_secret, db_cluster],
        ),
    )

    # APISIX routing for the Keycloak hostname.
    k8s.apiextensions.CustomResource(
        "keycloak-apisix-route",
        api_version="apisix.apache.org/v2",
        kind="ApisixRoute",
        metadata={"name": "keycloak-route", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "http": [
                {
                    "name": "keycloak",
                    "match": {
                        "hosts": [keycloak_hostname],
                        "paths": ["/*"],
                    },
                    "backends": [
                        {"serviceName": "keycloak-service", "servicePort": 8080}
                    ],
                    "plugins": [
                        {
                            "name": "proxy-rewrite",
                            "enable": True,
                            "config": {"scheme": "http"},
                        }
                    ],
                }
            ],
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, instance]),
    )

    k8s.apiextensions.CustomResource(
        "keycloak-apisix-tls",
        api_version="apisix.apache.org/v2",
        kind="ApisixTls",
        metadata={"name": "keycloak-tls", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "hosts": [keycloak_hostname],
            "secret": {"name": "local-dev-tls", "namespace": "local-infra"},
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, tls_secret]),
    )

    return IdentityCoreResources(
        operator_crds=operator_crds,
        operator=operator,
        instance=instance,
        admin_secret=admin_secret,
    )
