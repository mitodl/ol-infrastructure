"""
MIT Learn Local Dev — Applications Infrastructure Pulumi Stack.

Provisions resources that depend on the core infrastructure stack being deployed first:
  - Keycloak olapps realm (depends on Keycloak instance from core)

This stack should be deployed after the core stack is ready.
"""

import os
import sys
from pathlib import Path
from typing import Any

import pulumi_keycloak as keycloak
import pulumi_kubernetes as k8s

# Add parent directory to sys.path to import shared modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.helpers import make_resource_opts
from modules.keycloak import create_olapps_dev_realm
from pulumi import Config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = Config()

kubeconfig_path = config.get("kubeconfig") or ""

# Root domain configuration
root_domain = config.get("root_domain") or os.environ.get(
    "LOCAL_DEV_ROOT_DOMAIN", "mit.dev"
)

keycloak_hostname = config.get("keycloak_hostname") or f"sso.ol.{root_domain}"
keycloak_url = config.get("keycloak_url") or f"https://{keycloak_hostname}"

# Client secrets for OIDC configuration
mitlearn_client_secret = config.require_secret("mitlearn_client_secret")
learn_ai_client_secret = config.require_secret("learn_ai_client_secret")
mitxonline_client_secret = config.require_secret("mitxonline_client_secret")
unified_ecommerce_client_secret = config.require_secret(
    "unified_ecommerce_client_secret"
)

# ---------------------------------------------------------------------------
# Kubernetes provider
# ---------------------------------------------------------------------------

k8s_provider_opts: dict[str, Any] = {}
if kubeconfig_path:
    k8s_provider_opts["kubeconfig"] = kubeconfig_path

k8s_provider = k8s.Provider("k3d-local", **k8s_provider_opts)
_k8s = make_resource_opts(k8s_provider)

# ---------------------------------------------------------------------------
# Get local-infra namespace (already created by core stack)
# ---------------------------------------------------------------------------

local_infra_ns = k8s.core.v1.Namespace.get(
    "local-infra",
    id="local-infra",
    opts=_k8s(),
)

# ---------------------------------------------------------------------------
# Keycloak realm (depends on Keycloak instance from core stack)
# ---------------------------------------------------------------------------

keycloak_provider = keycloak.Provider(
    "keycloak-local",
    url=keycloak_url,
    realm="master",
    client_id="admin-cli",  # built-in admin client for username/password auth
    username="admin",
    password="admin",  # noqa: S106  # pragma: allowlist secret
    tls_insecure_skip_verify=True,  # mkcert CA not trusted by provider runtime
    initial_login=False,  # Keycloak must be ready before realm resources run
)

create_olapps_dev_realm(
    keycloak_provider=keycloak_provider,
    keycloak_url=keycloak_url,
    k8s_provider=k8s_provider,
    mitlearn_client_secret=mitlearn_client_secret,
    learn_ai_client_secret=learn_ai_client_secret,
    mitxonline_client_secret=mitxonline_client_secret,
    unified_ecommerce_client_secret=unified_ecommerce_client_secret,
)

# ---------------------------------------------------------------------------
# Stack outputs
# ---------------------------------------------------------------------------

# No outputs for apps-infra; realm configuration is consumed by cluster applications
