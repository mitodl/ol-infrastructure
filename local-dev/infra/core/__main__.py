"""
MIT Learn Local Dev — Core Infrastructure Pulumi Stack.

Provisions the foundational layer for the local k3d development environment:
  - Namespaces
  - TLS/cert-manager
  - APISIX ingress controller (Helm)
  - CNPG operator + cluster (database)
  - Keycloak operator and instance
  - Cache (Valkey StatefulSet)
  - Search (OpenSearch Helm)
  - AI services (Qdrant, Tika, LiteLLM)
  - Messaging (Mailpit)

The apps-infra stack depends on this core stack being deployed first.
It provisions the Keycloak realm and any other app-specific resources.
"""

import os
import sys
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as k8s

# Add parent directory to sys.path to import shared modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.ai import create_ai_services
from modules.cache import create_cache
from modules.database import create_database
from modules.helpers import make_resource_opts
from modules.identity_core import create_identity_core
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

# Root domain for all local-dev hostnames.
root_domain = config.get("root_domain") or os.environ.get(
    "LOCAL_DEV_ROOT_DOMAIN", "mit.dev"
)

keycloak_hostname = config.get("keycloak_hostname") or f"sso.ol.{root_domain}"
keycloak_url = config.get("keycloak_url") or f"https://{keycloak_hostname}"

apisix_admin_key = config.require_secret("apisix_admin_key")
apisix_viewer_key = config.require_secret("apisix_viewer_key")

cert_manager_version = config.get("cert_manager_version") or "v1.16.2"
cnpg_version = config.get("cnpg_version") or "0.23.0"
apisix_version = config.get("apisix_version") or "2.13.0"
keycloak_operator_version = config.get("keycloak_operator_version") or "26.0.7"

_infra_dir = Path(__file__).parent.parent
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

# ---------------------------------------------------------------------------
# In-cluster DNS override
#
# Public hostnames (sso.ol.<domain>, api.learn.<domain>, etc.) only exist in
# the developer's /etc/hosts, where they point at 127.0.0.1. Inside a pod that
# resolves to the pod's own loopback, so server-to-server calls over those
# hostnames -- APISIX fetching the Keycloak discovery/token endpoints, and
# cross-app API calls -- fail with "connection refused".
#
# k3s mounts a `coredns-custom` ConfigMap into CoreDNS and imports `*.server`
# files as sibling server blocks. We answer every name in the root domain zone
# with the APISIX gateway ClusterIP so in-cluster traffic enters through the
# same ingress the browser uses (TLS + routing handled by APISIX).
# ---------------------------------------------------------------------------
apisix_gateway = k8s.core.v1.Service.get(
    "apisix-gateway-lookup",
    id="operations/apache-apisix-gateway",
    opts=_k8s(depends_on=[ingress.apisix]),
)

k8s.core.v1.ConfigMap(
    "coredns-custom",
    metadata={"name": "coredns-custom", "namespace": "kube-system"},
    data={
        f"{root_domain}.server": apisix_gateway.spec.cluster_ip.apply(
            lambda ip: (
                f"{root_domain}:53 {{\n"
                "    errors\n"
                "    cache 30\n"
                "    template IN A {\n"
                f'        answer "{{{{ .Name }}}} 60 IN A {ip}"\n'
                "    }\n"
                # Answer AAAA with NODATA (NOERROR, no answer) instead of
                # letting the query fall through to SERVFAIL. glibc
                # getaddrinfo issues parallel A+AAAA lookups; a SERVFAIL on
                # the AAAA leg surfaces as EAI_AGAIN even when A succeeds.
                "    template IN AAAA {\n"
                "        rcode NOERROR\n"
                "    }\n"
                "}\n"
            )
        ),
    },
    opts=_k8s(depends_on=[ingress.apisix]),
)

create_cache(_k8s, namespaces["local-infra"])

search = create_search(_k8s, namespaces["local-infra"])

create_ai_services(_k8s, namespaces["local-infra"], None, _infra_dir)

messaging = create_messaging(_k8s, namespaces["local-infra"])

# Create database cluster (needed by Keycloak instance) before identity
db = create_database(_k8s, namespaces["local-infra"], cnpg_version)

# Deploy Keycloak operator and instance (but not realm — that's in apps-infra)
identity = create_identity_core(
    _k8s=_k8s,
    k8s_provider=k8s_provider,
    local_infra_ns=namespaces["local-infra"],
    apisix_release=ingress.apisix,
    tls_secret=tls.tls_secret,
    keycloak_operator_version=keycloak_operator_version,
    keycloak_hostname=keycloak_hostname,
    keycloak_url=keycloak_url,
    root_domain=root_domain,
    db_cluster=db.cluster,
)

# ---------------------------------------------------------------------------
# Stack outputs (consumed by app Tiltfiles and apps-infra stack)
# ---------------------------------------------------------------------------

pulumi.export("keycloak_url", keycloak_url)
pulumi.export("keycloak_hostname", keycloak_hostname)
pulumi.export("postgres_host", "local-pg-rw.local-infra.svc.cluster.local")
pulumi.export("valkey_host", "valkey.local-infra.svc.cluster.local")
pulumi.export("qdrant_url", "http://qdrant.local-infra.svc.cluster.local:6333")
pulumi.export(
    "opensearch_url",
    "http://opensearch-cluster-master.local-infra.svc.cluster.local:9200",
)
pulumi.export("tika_url", "http://tika.local-infra.svc.cluster.local:9998")
pulumi.export("litellm_url", "http://litellm.local-infra.svc.cluster.local:4000")
pulumi.export("mailpit_ui_url", "http://mailpit.local-infra.svc.cluster.local:8025")
