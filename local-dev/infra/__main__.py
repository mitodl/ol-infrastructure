"""
MIT Learn Local Dev — Shared Infrastructure Pulumi Stack.

Provisions all in-cluster shared services for the local k3d development
environment. This stack owns every in-cluster resource; setup.sh owns only
the cluster itself, mkcert certificates, and /etc/hosts entries.

Services provisioned:
  - cert-manager (Helm)
  - CloudNativePG operator (Helm) + shared PostgreSQL Cluster
  - Valkey StatefulSet
  - APISIX (Helm, traditional mode) in operations namespace
  - Qdrant (vector DB) Deployment
  - OpenSearch (Helm) for search
  - Tika (document parsing) Deployment
  - LiteLLM (AI proxy) Deployment
  - Mailpit (local SMTP) Deployment
  - Keycloak Operator (official keycloak-k8s-resources) + Keycloak instance
  - Keycloak olapps realm via pulumi-keycloak provider
"""

import os
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
from modules.ai import create_ai_services
from modules.cache import create_cache
from modules.database import create_database
from modules.helpers import make_resource_opts
from modules.identity import create_identity
from modules.ingress import create_ingress
from modules.messaging import create_messaging
from modules.namespaces import create_namespaces
from modules.search import create_search
from modules.tls import create_tls_resources
from pulumi import Config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = Config()

kubeconfig_path = config.get("kubeconfig") or ""
tls_cert_path = config.get("tls_cert_path") or "local-dev/certs/local-dev.pem"
tls_key_path = config.get("tls_key_path") or "local-dev/certs/local-dev-key.pem"
mkcert_ca_cert_path = config.get("mkcert_ca_cert_path") or "local-dev/certs/rootCA.pem"

# Root domain for all local-dev hostnames.  Override via the LOCAL_DEV_ROOT_DOMAIN
# environment variable or the Pulumi config key `root_domain`.  The default
# `mit.dev` matches the baseline setup.sh / /etc/hosts configuration.
root_domain = config.get("root_domain") or os.environ.get(
    "LOCAL_DEV_ROOT_DOMAIN", "mit.dev"
)

keycloak_hostname = config.get("keycloak_hostname") or f"sso.ol.{root_domain}"
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
apisix_version = config.get("apisix_version") or "2.13.0"
keycloak_operator_version = config.get("keycloak_operator_version") or "26.0.7"

_infra_dir = Path(__file__).parent
_repo_root = _infra_dir.parent.parent
_cert_path = _repo_root / tls_cert_path
_key_path = _repo_root / tls_key_path
_ca_cert_path = _repo_root / mkcert_ca_cert_path

# ---------------------------------------------------------------------------
# Kubernetes provider
# ---------------------------------------------------------------------------

k8s_provider_opts: dict[str, Any] = {}
if kubeconfig_path:
    k8s_provider_opts["kubeconfig"] = kubeconfig_path

k8s_provider = k8s.Provider("k3d-local", **k8s_provider_opts)
_k8s = make_resource_opts(k8s_provider)

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

namespaces = create_namespaces(_k8s)

tls = create_tls_resources(
    _k8s,
    namespaces=namespaces,
    cert_path=_cert_path,
    key_path=_key_path,
    ca_cert_path=_ca_cert_path,
)

ingress = create_ingress(
    _k8s,
    namespaces=namespaces,
    apisix_admin_key=apisix_admin_key,
    apisix_viewer_key=apisix_viewer_key,
    cert_manager_version=cert_manager_version,
    apisix_version=apisix_version,
)

db = create_database(_k8s, namespaces["local-infra"], cnpg_version)

create_cache(_k8s, namespaces["local-infra"])

search = create_search(_k8s, namespaces["local-infra"])

create_ai_services(_k8s, namespaces["local-infra"], db.cluster, _infra_dir)

messaging = create_messaging(_k8s, namespaces["local-infra"])

create_identity(
    _k8s=_k8s,
    k8s_provider=k8s_provider,
    local_infra_ns=namespaces["local-infra"],
    pg_cluster=db.cluster,
    mailpit_deployment=messaging.deployment,
    apisix_release=ingress.apisix,
    tls_secret=tls.tls_secret,
    keycloak_operator_version=keycloak_operator_version,
    keycloak_hostname=keycloak_hostname,
    keycloak_url=keycloak_url,
    mitlearn_client_secret=mitlearn_client_secret,
    learn_ai_client_secret=learn_ai_client_secret,
    mitxonline_client_secret=mitxonline_client_secret,
    unified_ecommerce_client_secret=unified_ecommerce_client_secret,
    apisix_oidc_session_secret=apisix_oidc_session_secret,
)

# ---------------------------------------------------------------------------
# Stack outputs (consumed by app Tiltfiles)
# ---------------------------------------------------------------------------

pulumi.export("keycloak_url", keycloak_url)
pulumi.export("valkey_host", "valkey.local-infra.svc.cluster.local")
pulumi.export("postgres_host", "local-pg-rw.local-infra.svc.cluster.local")
pulumi.export("qdrant_url", "http://qdrant.local-infra.svc.cluster.local:6333")
pulumi.export(
    "opensearch_url", "http://opensearch-master.local-infra.svc.cluster.local:9200"
)
pulumi.export("tika_url", "http://tika.local-infra.svc.cluster.local:9998")
pulumi.export("litellm_url", "http://litellm.local-infra.svc.cluster.local:4000")
pulumi.export("mailpit_ui_url", "http://mailpit.local-infra.svc.cluster.local:8025")
