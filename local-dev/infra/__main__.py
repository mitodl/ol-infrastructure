"""
MIT Learn Local Dev — Shared Infrastructure Pulumi Stack.

Provisions all in-cluster shared services for the local k3d development
environment. This stack owns every in-cluster resource; setup.sh owns only
the cluster itself, mkcert certificates, and /etc/hosts entries.

Services provisioned:
  - cert-manager (Helm) — future certificate management
  - CloudNativePG operator (Helm) + shared PostgreSQL Cluster
  - Redis StatefulSet
  - APISIX (Helm, standalone mode) in operations namespace
  - Qdrant (vector DB) Deployment
  - OpenSearch (Helm) for search
  - Tika (document parsing) Deployment
  - LiteLLM (AI proxy) Deployment
  - Mailpit (local SMTP) Deployment
  - Keycloak Operator (official, from keycloak-k8s-resources) + Keycloak instance
  - Keycloak olapps realm (Phase 3) via pulumi-keycloak provider
"""

import base64
from pathlib import Path
from typing import Any

import pulumi
import pulumi_keycloak as keycloak
import pulumi_kubernetes as k8s
from local_dev_keycloak import create_olapps_dev_realm
from pulumi import Config, Output, ResourceOptions

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = Config()

kubeconfig_path = config.get("kubeconfig") or ""
tls_cert_path = config.get("tls_cert_path") or "local-dev/certs/local-dev.pem"
tls_key_path = config.get("tls_key_path") or "local-dev/certs/local-dev-key.pem"
mkcert_ca_cert_path = config.get("mkcert_ca_cert_path") or "local-dev/certs/rootCA.pem"

keycloak_hostname = config.get("keycloak_hostname") or "sso.ol.mit.dev"
keycloak_url = config.get("keycloak_url") or f"https://{keycloak_hostname}"

mitlearn_client_secret = config.require_secret("mitlearn_client_secret")
learn_ai_client_secret = config.require_secret("learn_ai_client_secret")
mitxonline_client_secret = config.require_secret("mitxonline_client_secret")
unified_ecommerce_client_secret = config.require_secret(
    "unified_ecommerce_client_secret"
)
apisix_admin_key = config.require_secret("apisix_admin_key")
apisix_viewer_key = config.require_secret("apisix_viewer_key")
apisix_oidc_session_secret = config.require_secret("apisix_oidc_session_secret")

cert_manager_version = config.get("cert_manager_version") or "v1.16.2"
cnpg_version = config.get("cnpg_version") or "0.23.0"
apisix_version = config.get("apisix_version") or "2.12.0"
keycloak_operator_version = config.get("keycloak_operator_version") or "26.0.7"

# Resolve certificate file paths relative to repo root (two levels up from
# local-dev/infra/).
_infra_dir = Path(__file__).parent
_repo_root = _infra_dir.parent.parent
_cert_path = _repo_root / tls_cert_path
_key_path = _repo_root / tls_key_path
_ca_cert_path = _repo_root / mkcert_ca_cert_path

# ---------------------------------------------------------------------------
# Kubernetes Provider
# ---------------------------------------------------------------------------

k8s_provider_opts: dict[str, Any] = {}
if kubeconfig_path:
    k8s_provider_opts["kubeconfig"] = kubeconfig_path

k8s_provider = k8s.Provider(
    "k3d-local",
    **k8s_provider_opts,
)

_k8s_opts = ResourceOptions(provider=k8s_provider)


def _k8s(parent=None, depends_on=None) -> ResourceOptions:
    """Return ResourceOptions scoped to the local k8s provider."""
    return ResourceOptions(
        provider=k8s_provider,
        parent=parent,
        depends_on=depends_on or [],
    )


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

local_infra_ns = k8s.core.v1.Namespace(
    "local-infra",
    metadata={"name": "local-infra"},
    opts=_k8s(),
)

operations_ns = k8s.core.v1.Namespace(
    "operations",
    metadata={"name": "operations"},
    opts=_k8s(),
)

# App namespaces — created here so infra-owned Secrets (OIDC creds) can be
# placed into them before the app Tiltfile runs.
_app_namespaces = {}
for _ns_name in ("mit-learn", "learn-ai", "mitxonline", "odl-video-service"):
    _app_namespaces[_ns_name] = k8s.core.v1.Namespace(
        f"ns-{_ns_name}",
        metadata={"name": _ns_name},
        opts=_k8s(),
    )

# ---------------------------------------------------------------------------
# TLS Secrets
# ---------------------------------------------------------------------------
# mkcert has already signed the wildcard cert (setup.sh).
# We copy the cert+key into every namespace that needs TLS — APISIX resolves
# ApisixTls by looking up the Secret in the route's namespace.
# No cert-manager ClusterIssuer is needed: mkcert already did the signing.


# Read cert files at Pulumi evaluation time. These files are created by
# setup.sh before `pulumi up` is run.
def _read_file_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


_tls_cert_b64 = _read_file_b64(_cert_path)
_tls_key_b64 = _read_file_b64(_key_path)


def _tls_secret(resource_name: str, namespace: str, parent) -> k8s.core.v1.Secret:
    return k8s.core.v1.Secret(
        resource_name,
        metadata={"name": "local-dev-tls", "namespace": namespace},
        type="kubernetes.io/tls",
        data={"tls.crt": _tls_cert_b64, "tls.key": _tls_key_b64},
        opts=_k8s(parent=parent),
    )


tls_secret = _tls_secret("local-dev-tls", "local-infra", local_infra_ns)
tls_secret_ops = _tls_secret("local-dev-tls-operations", "operations", operations_ns)
# Per-app-namespace copies — required for ApisixTls in those namespaces.
tls_secret_mit_learn = _tls_secret(
    "local-dev-tls-mit-learn", "mit-learn", _app_namespaces["mit-learn"]
)
tls_secret_learn_ai = _tls_secret(
    "local-dev-tls-learn-ai", "learn-ai", _app_namespaces["learn-ai"]
)
tls_secret_mitxonline = _tls_secret(
    "local-dev-tls-mitxonline", "mitxonline", _app_namespaces["mitxonline"]
)
tls_secret_odl_video = _tls_secret(
    "local-dev-tls-odl-video-service",
    "odl-video-service",
    _app_namespaces["odl-video-service"],
)

# ---------------------------------------------------------------------------
# cert-manager (Helm) — installed for future use; not used for mkcert signing
# ---------------------------------------------------------------------------

cert_manager_release = k8s.helm.v3.Release(
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

# ---------------------------------------------------------------------------
# CloudNativePG Operator + shared PostgreSQL Cluster
# ---------------------------------------------------------------------------

cnpg_release = k8s.helm.v3.Release(
    "cnpg-operator",
    k8s.helm.v3.ReleaseArgs(
        name="cnpg",
        chart="cloudnative-pg",
        version=cnpg_version,
        namespace="local-infra",
        repository_opts=k8s.helm.v3.RepositoryOptsArgs(
            repo="https://cloudnative-pg.github.io/charts",
        ),
        cleanup_on_fail=True,
        values={
            "replicaCount": 1,
            "resources": {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"memory": "256Mi"},
            },
        },
    ),
    opts=_k8s(parent=local_infra_ns),
)

# A fixed-password credential Secret used by CNPG and by app DATABASE_URLs.
# This avoids needing to read a CNPG-generated random password from a separate
# Secret at deploy time.
pg_credentials = k8s.core.v1.Secret(
    "pg-credentials",
    metadata={"name": "pg-app-credentials", "namespace": "local-infra"},
    string_data={"username": "app", "password": "localdev"},  # pragma: allowlist secret
    opts=_k8s(parent=local_infra_ns),
)

# Shared PostgreSQL cluster — initdb creates all app databases in one cluster.
# Each app uses a dedicated database within this cluster.
pg_cluster = k8s.apiextensions.CustomResource(
    "local-pg-cluster",
    api_version="postgresql.cnpg.io/v1",
    kind="Cluster",
    metadata={
        "name": "local-pg",
        "namespace": "local-infra",
    },
    spec={
        "instances": 1,
        "storage": {
            "size": "10Gi",
        },
        "bootstrap": {
            "initdb": {
                "database": "app",
                "owner": "app",
                # Reference the fixed-password Secret so apps can use a known URL.
                "secret": {"name": "pg-app-credentials"},
                # Create all app databases owned by `app` so Django migrations succeed.
                "postInitSQL": [
                    "CREATE DATABASE mitlearn OWNER app;",
                    "CREATE DATABASE learnai OWNER app;",
                    "CREATE DATABASE mitxonline OWNER app;",
                    "CREATE DATABASE odlvideo OWNER app;",
                    "CREATE DATABASE keycloak OWNER app;",
                    "CREATE DATABASE litellm OWNER app;",
                ],
            }
        },
        "postgresql": {
            "parameters": {
                "max_connections": "100",
            },
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"memory": "512Mi"},
        },
    },
    opts=_k8s(parent=local_infra_ns, depends_on=[cnpg_release, pg_credentials]),
)

# ---------------------------------------------------------------------------
# Redis StatefulSet
# ---------------------------------------------------------------------------

redis_sts = k8s.apps.v1.StatefulSet(
    "redis",
    metadata={"name": "redis", "namespace": "local-infra"},
    spec={
        "replicas": 1,
        "selector": {"matchLabels": {"app": "redis"}},
        "serviceName": "redis",
        "template": {
            "metadata": {"labels": {"app": "redis"}},
            "spec": {
                "containers": [
                    {
                        "name": "redis",
                        "image": "redis:7-alpine",
                        "ports": [{"containerPort": 6379}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"memory": "128Mi"},
                        },
                    }
                ]
            },
        },
    },
    opts=_k8s(parent=local_infra_ns),
)

redis_svc = k8s.core.v1.Service(
    "redis-svc",
    metadata={"name": "redis", "namespace": "local-infra"},
    spec={
        "selector": {"app": "redis"},
        "ports": [{"port": 6379, "targetPort": 6379}],
        "clusterIP": "None",  # headless for StatefulSet
    },
    opts=_k8s(parent=redis_sts),
)

# ---------------------------------------------------------------------------
# APISIX (standalone mode, operations namespace)
# ---------------------------------------------------------------------------

apisix_release = k8s.helm.v3.Release(
    "apisix",
    k8s.helm.v3.ReleaseArgs(
        name="apache-apisix",
        chart="apisix",
        version=apisix_version,
        namespace="operations",
        repository_opts=k8s.helm.v3.RepositoryOptsArgs(
            repo="https://apache.github.io/apisix-helm-chart",
        ),
        cleanup_on_fail=True,
        values=Output.all(
            admin=apisix_admin_key,
            viewer=apisix_viewer_key,
        ).apply(
            lambda args: {
                "service": {
                    "type": "NodePort",
                    "http": {
                        "enabled": True,
                        "servicePort": 80,
                        "containerPort": 9080,
                        "nodePort": 30080,
                    },
                    "tls": {
                        "enabled": True,
                        "servicePort": 443,
                        "containerPort": 9443,
                        "nodePort": 30443,
                    },
                },
                "apisix": {
                    "deployment": {
                        "mode": "standalone",
                        "role": "traditional",
                        "role_traditional": {
                            "config_provider": "yaml",
                        },
                    },
                    "ssl": {
                        "enabled": True,
                        "listenPort": 9443,
                    },
                    "admin": {
                        "enabled": True,
                        "credentials": {
                            "admin": args["admin"],
                            "viewer": args["viewer"],
                        },
                    },
                },
                "ingress-controller": {
                    "enabled": True,
                    "config": {
                        "apisix": {
                            "serviceName": "apache-apisix-admin",
                            "serviceNamespace": "operations",
                        },
                        "kubernetes": {
                            "ingressClass": "apache-apisix",
                        },
                    },
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"memory": "512Mi"},
                },
                "autoscaling": {"enabled": False},
                "replicaCount": 1,
            }
        ),
    ),
    opts=_k8s(parent=operations_ns),
)

# ---------------------------------------------------------------------------
# Qdrant (vector database)
# ---------------------------------------------------------------------------

qdrant_deployment = k8s.apps.v1.Deployment(
    "qdrant",
    metadata={"name": "qdrant", "namespace": "local-infra"},
    spec={
        "replicas": 1,
        "selector": {"matchLabels": {"app": "qdrant"}},
        "template": {
            "metadata": {"labels": {"app": "qdrant"}},
            "spec": {
                "containers": [
                    {
                        "name": "qdrant",
                        "image": "qdrant/qdrant:v1.12.5",
                        "ports": [
                            {"containerPort": 6333, "name": "http"},
                            {"containerPort": 6334, "name": "grpc"},
                        ],
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"memory": "512Mi"},
                        },
                        "volumeMounts": [
                            {"name": "qdrant-storage", "mountPath": "/qdrant/storage"}
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "qdrant-storage",
                        "emptyDir": {},
                    }
                ],
            },
        },
    },
    opts=_k8s(parent=local_infra_ns),
)

k8s.core.v1.Service(
    "qdrant-svc",
    metadata={"name": "qdrant", "namespace": "local-infra"},
    spec={
        "selector": {"app": "qdrant"},
        "ports": [
            {"name": "http", "port": 6333, "targetPort": 6333},
            {"name": "grpc", "port": 6334, "targetPort": 6334},
        ],
    },
    opts=_k8s(parent=qdrant_deployment),
)

# ---------------------------------------------------------------------------
# OpenSearch (Helm)  # noqa: ERA001
# ---------------------------------------------------------------------------

opensearch_release = k8s.helm.v3.Release(
    "opensearch",
    k8s.helm.v3.ReleaseArgs(
        name="opensearch",
        chart="opensearch",
        version="2.26.1",
        namespace="local-infra",
        repository_opts=k8s.helm.v3.RepositoryOptsArgs(
            repo="https://opensearch-project.github.io/helm-charts",
        ),
        cleanup_on_fail=True,
        values={
            "singleNode": True,
            "replicas": 1,
            "opensearchJavaOpts": "-Xms256m -Xmx256m",
            "resources": {
                "requests": {"cpu": "100m", "memory": "512Mi"},
                "limits": {"memory": "1Gi"},
            },
            "persistence": {"size": "5Gi"},
            # Disable security for local dev.
            "config": {"opensearch.yml": "plugins.security.disabled: true\n"},
            "extraEnvs": [
                {"name": "DISABLE_INSTALL_DEMO_CONFIG", "value": "true"},
                {"name": "DISABLE_SECURITY_PLUGIN", "value": "true"},
            ],
        },
    ),
    opts=_k8s(parent=local_infra_ns),
)

# ---------------------------------------------------------------------------
# Tika (document parsing)
# ---------------------------------------------------------------------------

tika_deployment = k8s.apps.v1.Deployment(
    "tika",
    metadata={"name": "tika", "namespace": "local-infra"},
    spec={
        "replicas": 1,
        "selector": {"matchLabels": {"app": "tika"}},
        "template": {
            "metadata": {"labels": {"app": "tika"}},
            "spec": {
                "containers": [
                    {
                        "name": "tika",
                        "image": "apache/tika:3.0.0.0",
                        "ports": [{"containerPort": 9998}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "256Mi"},
                            "limits": {"memory": "512Mi"},
                        },
                    }
                ]
            },
        },
    },
    opts=_k8s(parent=local_infra_ns),
)

k8s.core.v1.Service(
    "tika-svc",
    metadata={"name": "tika", "namespace": "local-infra"},
    spec={
        "selector": {"app": "tika"},
        "ports": [{"port": 9998, "targetPort": 9998}],
    },
    opts=_k8s(parent=tika_deployment),
)

# ---------------------------------------------------------------------------
# LiteLLM (AI proxy / model router)
# ---------------------------------------------------------------------------

litellm_config_cm = k8s.core.v1.ConfigMap(
    "litellm-config",
    metadata={"name": "litellm-config", "namespace": "local-infra"},
    data={
        "config.yaml": (
            Path(__file__).parent / "config" / "litellm-config.yaml"
        ).read_text(),
    },
    opts=_k8s(parent=local_infra_ns),
)

litellm_deployment = k8s.apps.v1.Deployment(
    "litellm",
    metadata={"name": "litellm", "namespace": "local-infra"},
    spec={
        "replicas": 1,
        "selector": {"matchLabels": {"app": "litellm"}},
        "template": {
            "metadata": {"labels": {"app": "litellm"}},
            "spec": {
                "containers": [
                    {
                        "name": "litellm",
                        "image": "ghcr.io/berriai/litellm:main-stable",
                        "args": [
                            "--config",
                            "/etc/litellm/config.yaml",
                            "--port",
                            "4000",
                        ],
                        "ports": [{"containerPort": 4000}],
                        "env": [
                            {
                                "name": "OPENAI_API_KEY",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "litellm-secrets",
                                        "key": "openai_api_key",
                                        "optional": True,
                                    }
                                },
                            }
                        ],
                        "volumeMounts": [
                            {
                                "name": "config",
                                "mountPath": "/etc/litellm",
                                "readOnly": True,
                            }
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "128Mi"},
                            "limits": {"memory": "512Mi"},
                        },
                    }
                ],
                "volumes": [
                    {
                        "name": "config",
                        "configMap": {"name": "litellm-config"},
                    }
                ],
            },
        },
    },
    opts=_k8s(parent=local_infra_ns, depends_on=[litellm_config_cm, pg_cluster]),
)

k8s.core.v1.Service(
    "litellm-svc",
    metadata={"name": "litellm", "namespace": "local-infra"},
    spec={
        "selector": {"app": "litellm"},
        "ports": [{"port": 4000, "targetPort": 4000}],
    },
    opts=_k8s(parent=litellm_deployment),
)

# ---------------------------------------------------------------------------
# Mailpit (local SMTP — prevents broken Keycloak email verification flows)
# ---------------------------------------------------------------------------

mailpit_deployment = k8s.apps.v1.Deployment(
    "mailpit",
    metadata={"name": "mailpit", "namespace": "local-infra"},
    spec={
        "replicas": 1,
        "selector": {"matchLabels": {"app": "mailpit"}},
        "template": {
            "metadata": {"labels": {"app": "mailpit"}},
            "spec": {
                "containers": [
                    {
                        "name": "mailpit",
                        "image": "axllent/mailpit:latest",
                        "ports": [
                            {"containerPort": 1025, "name": "smtp"},
                            {"containerPort": 8025, "name": "ui"},
                        ],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "32Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    }
                ]
            },
        },
    },
    opts=_k8s(parent=local_infra_ns),
)

mailpit_svc = k8s.core.v1.Service(
    "mailpit-svc",
    metadata={"name": "mailpit", "namespace": "local-infra"},
    spec={
        "selector": {"app": "mailpit"},
        "ports": [
            {"name": "smtp", "port": 1025, "targetPort": 1025},
            {"name": "ui", "port": 8025, "targetPort": 8025},
        ],
    },
    opts=_k8s(parent=mailpit_deployment),
)

# ---------------------------------------------------------------------------
# Keycloak Operator (Helm) + Keycloak instance
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Keycloak Operator (official — keycloak-k8s-resources) + Keycloak instance
# ---------------------------------------------------------------------------
# Install the official Keycloak Operator from the upstream keycloak-k8s-resources
# GitHub releases. This is the same operator used in production.
#
# The release manifests include:
#   - CRDs: Keycloak, KeycloakRealmImport (cluster-scoped)
#   - Operator Deployment/RBAC in the target namespace
#
# Namespace transformation is applied to put the operator in local-infra.

_kc_version = keycloak_operator_version
_kc_base = f"https://raw.githubusercontent.com/keycloak/keycloak-k8s-resources/{_kc_version}/kubernetes"

keycloak_operator = k8s.yaml.v2.ConfigGroup(
    "keycloak-operator",
    files=[
        f"{_kc_base}/keycloaks.k8s.keycloak.org-v1.yml",
        f"{_kc_base}/keycloakrealmimports.k8s.keycloak.org-v1.yml",
        f"{_kc_base}/kubernetes.yml",
    ],
    # Redirect all namespaced resources to local-infra; CRDs are cluster-scoped
    # and will not be affected by this (they have no namespace field).
    transformations=[
        lambda obj, _opts: (
            obj["metadata"].__setitem__("namespace", "local-infra")
            if obj.get("metadata")
            and obj["kind"]
            not in ("ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition")
            else None
        )
    ],
    opts=_k8s(parent=local_infra_ns),
)

# Keycloak admin credentials Secret — referenced by the Keycloak CR.
keycloak_admin_secret = k8s.core.v1.Secret(
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

keycloak_instance = k8s.apiextensions.CustomResource(
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
        "ingress": {
            "enabled": False,  # We route through APISIX.
        },
        "features": {
            "enabled": ["organization"],
        },
        "additionalOptions": [
            # Use Mailpit for local email delivery.
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
                                {
                                    "name": "KC_HOSTNAME_STRICT",
                                    "value": "false",
                                },
                                {
                                    "name": "KC_PROXY",
                                    "value": "edge",
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
        depends_on=[
            keycloak_operator,
            pg_cluster,
            mailpit_deployment,
            keycloak_admin_secret,
        ],
    ),
)

# ---------------------------------------------------------------------------
# APISIX Route for Keycloak (SSO passthrough)
# ---------------------------------------------------------------------------

keycloak_apisix_route = k8s.apiextensions.CustomResource(
    "keycloak-apisix-route",
    api_version="apisix.apache.org/v2",
    kind="ApisixRoute",
    metadata={
        "name": "keycloak-route",
        "namespace": "local-infra",
    },
    spec={
        "http": [
            {
                "name": "keycloak",
                "match": {
                    "hosts": [keycloak_hostname],
                    "paths": ["/*"],
                },
                "backends": [
                    {
                        "serviceName": "keycloak-service",
                        "servicePort": 80,
                    }
                ],
                "plugins": [
                    {
                        "name": "proxy-rewrite",
                        "enable": True,
                        "config": {"scheme": "http"},
                    }
                ],
            }
        ]
    },
    opts=_k8s(
        parent=local_infra_ns,
        depends_on=[apisix_release, keycloak_instance],
    ),
)

# ApisixTls for Keycloak hostname.
keycloak_apisix_tls = k8s.apiextensions.CustomResource(
    "keycloak-apisix-tls",
    api_version="apisix.apache.org/v2",
    kind="ApisixTls",
    metadata={
        "name": "keycloak-tls",
        "namespace": "local-infra",
    },
    spec={
        "hosts": [keycloak_hostname],
        "secret": {
            "name": "local-dev-tls",
            "namespace": "local-infra",
        },
    },
    opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, tls_secret]),
)

# ---------------------------------------------------------------------------
# Keycloak olapps realm (Phase 3) — provisioned via pulumi-keycloak provider
# ---------------------------------------------------------------------------

# The Keycloak provider connects after the instance is ready.
# In practice, `pulumi up` may need to be run twice: once to stand up
# Keycloak, and once to provision the realm, or use `--target` scoping.
keycloak_provider = keycloak.Provider(
    "keycloak-local",
    url=keycloak_url,
    realm="master",
    username="admin",
    password="admin",  # noqa: S106  # pragma: allowlist secret
    initial_login=True,
    opts=ResourceOptions(depends_on=[keycloak_instance]),
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

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

pulumi.export("keycloak_url", keycloak_url)
pulumi.export("redis_host", "redis.local-infra.svc.cluster.local")
pulumi.export("postgres_host", "local-pg-rw.local-infra.svc.cluster.local")
pulumi.export("qdrant_url", "http://qdrant.local-infra.svc.cluster.local:6333")
pulumi.export(
    "opensearch_url", "http://opensearch-master.local-infra.svc.cluster.local:9200"
)
pulumi.export("tika_url", "http://tika.local-infra.svc.cluster.local:9998")
pulumi.export("litellm_url", "http://litellm.local-infra.svc.cluster.local:4000")
pulumi.export("mailpit_ui_url", "http://mailpit.local-infra.svc.cluster.local:8025")
