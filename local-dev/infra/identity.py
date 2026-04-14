"""Keycloak identity provider resources for the local-dev infra stack.

Provisions:
  - Keycloak Operator (official keycloak-k8s-resources manifests)
  - Keycloak CustomResource (CR) instance backed by the shared CNPG cluster
  - APISIX routing (ApisixRoute + ApisixTls) for the Keycloak hostname
  - olapps realm via the pulumi-keycloak provider (keycloak.py)
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_keycloak as keycloak
import pulumi_kubernetes as k8s
import requests
import yaml as pyyaml
from keycloak import create_olapps_dev_realm
from pulumi import Output, ResourceOptions


@dataclass
class IdentityResources:
    """Resources created by the identity (Keycloak) module."""

    operator_crds: k8s.yaml.v2.ConfigGroup
    operator: k8s.yaml.v2.ConfigGroup
    instance: k8s.apiextensions.CustomResource
    keycloak_provider: keycloak.Provider


def create_identity(  # noqa: PLR0913
    _k8s: Callable[..., ResourceOptions],
    k8s_provider: k8s.Provider,
    local_infra_ns: k8s.core.v1.Namespace,
    pg_cluster: k8s.apiextensions.CustomResource,
    mailpit_deployment: k8s.apps.v1.Deployment,
    apisix_release: k8s.helm.v3.Release,
    tls_secret: k8s.core.v1.Secret,
    keycloak_operator_version: str,
    keycloak_hostname: str,
    keycloak_url: str,
    mitlearn_client_secret: Output,
    learn_ai_client_secret: Output,
    mitxonline_client_secret: Output,
    unified_ecommerce_client_secret: Output,
    apisix_oidc_session_secret: Output,
) -> IdentityResources:
    """Deploy Keycloak operator, instance, routes, and olapps realm."""
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
            "hostname": {
                "hostname": keycloak_hostname,
            },
            "http": {
                "httpEnabled": True,
                "tlsSecret": "local-dev-tls",  # pragma: allowlist secret
            },
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
            "features": {"enabled": ["organization"]},
            "additionalOptions": [
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
                "requests": {"cpu": "200m", "memory": "512Mi"},
                "limits": {"memory": "1Gi"},
            },
            "unsupported": {
                "podTemplate": {
                    "spec": {
                        "containers": [
                            {
                                "name": "keycloak",
                                "env": [
                                    {"name": "KC_HOSTNAME_STRICT", "value": "false"},
                                    {"name": "KC_PROXY", "value": "edge"},
                                ],
                            }
                        ]
                    }
                }
            },
        },
        opts=_k8s(
            parent=local_infra_ns,
            depends_on=[operator, pg_cluster, mailpit_deployment, admin_secret],
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

    # Keycloak provider + olapps realm.
    keycloak_provider = keycloak.Provider(
        "keycloak-local",
        url=keycloak_url,
        realm="master",
        client_id="admin-cli",  # built-in admin client for username/password auth
        username="admin",
        password="admin",  # noqa: S106  # pragma: allowlist secret
        tls_insecure_skip_verify=True,  # mkcert CA not trusted by provider runtime
        initial_login=False,  # Keycloak must be ready before realm resources run
        opts=ResourceOptions(depends_on=[instance]),
    )

    create_olapps_dev_realm(
        keycloak_provider=keycloak_provider,
        keycloak_url=keycloak_url,
        k8s_provider=k8s_provider,
        mitlearn_client_secret=mitlearn_client_secret,
        learn_ai_client_secret=learn_ai_client_secret,
        mitxonline_client_secret=mitxonline_client_secret,
        unified_ecommerce_client_secret=unified_ecommerce_client_secret,
        apisix_oidc_session_secret=apisix_oidc_session_secret,
    )

    return IdentityResources(
        operator_crds=operator_crds,
        operator=operator,
        instance=instance,
        keycloak_provider=keycloak_provider,
    )
