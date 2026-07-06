"""Deploy the software-engineering agent-class MCP workloads on the operations cluster.

This stack owns the ``toolhive-swe`` namespace: the ``MCPServer``, ``MCPGroup``, and
``VirtualMCPServer`` resources consumed by SWE/platform agents. The ToolHive operator
and CRDs that reconcile these resources are installed cluster-scoped by the
``ol-application-toolhive-operator`` stack; this stack references that one so it fails
fast if the operator has never been deployed.

See ``../toolhive-operator/DEPLOYMENT_STRATEGY.md`` for why agent classes are
separated by namespace under a single operator.

This is the initial CI-only deployment. It wires up ToolHive resources following
https://docs.stacklok.com/toolhive/guides-vmcp/quickstart and
https://docs.stacklok.com/toolhive/guides-vmcp/authentication:

- an ``MCPGroup`` (``swe-tools``) that groups the SWE backend MCP servers,
- the reference ``fetch`` ``MCPServer``
  (https://docs.stacklok.com/toolhive/guides-mcp/fetch), joined to that group via
  ``spec.groupRef``,
- an ``MCPOIDCConfig`` (``swe-vmcp-oidc``) used to validate the JWTs the vMCP's own
  embedded auth server issues (its issuer is the vMCP endpoint itself), and
- a ``VirtualMCPServer`` (``swe-vmcp``) that aggregates every backend in the group
  behind a single endpoint and fronts them with an embedded OAuth authorization
  server.

Incoming auth (browser login via Keycloak, brokered by ToolHive):
    Authentication uses ToolHive's EMBEDDED authorization server
    (``spec.authServerConfig``). The vMCP is the OAuth provider that MCP clients talk
    to: it exposes ``/authorize``, ``/token``, ``/register`` and
    ``/.well-known/oauth-authorization-server`` at its own URL and BROKERS interactive
    login to Keycloak (the ``ol-platform-engineering`` realm) as an upstream OIDC
    provider. ``spec.incomingAuth`` then validates the JWTs the embedded server issues
    (issuer == the vMCP endpoint) and advertises the protected-resource metadata
    (RFC 9728) that points clients at the embedded auth server.

    The end-to-end flow for a client such as Claude Code: hit the endpoint → get a
    401 pointing at the vMCP's own auth server → the client registers itself via
    Dynamic Client Registration (RFC 7591), so NO pre-registered client_id is needed
    on the client side (just the URL) → a browser opens to the vMCP's ``/authorize``,
    which redirects to Keycloak for login → Keycloak redirects back to the vMCP's
    ``/oauth/callback`` → the vMCP mints its own JWT → the client retries with that
    bearer token, which ``incomingAuth`` validates.

    Keycloak sees ONE ordinary CONFIDENTIAL web-app client (``ol-toolhive-client``,
    provisioned by the keycloak substructure) whose secret is synced from Vault
    (``secret-operations/sso/toolhive``) into this namespace by the Vault Secrets
    Operator and referenced as the upstream provider's ``clientSecretRef``. No
    Keycloak Dynamic Client Registration is required — DCR happens against the vMCP,
    not Keycloak.

    APISIX does NOT participate in authentication — it only terminates TLS and proxies
    every path (``/mcp``, ``/authorize``, ``/token``, ``/oauth/callback``,
    ``/.well-known/*``) through to the vMCP Service.

    Two pieces of state must persist for clients to stay authenticated across vMCP
    pod restarts, and both are provisioned here:
      * Signing material — ``authServerConfig.signingKeySecretRefs`` (an RSA-2048
        PKCS#8 PEM key) and ``hmacSecretRefs`` (a 256-bit base64 HMAC), read from
        encrypted stack config (``toolhive_swe:auth_server_signing_key`` /
        ``:auth_server_hmac_secret``) so they are stable across deploys. Without these
        the auth server uses ephemeral keys and previously issued tokens break on
        restart.
      * Session + DCR registration store — ``authServerConfig.storage.redis`` points
        at a small single-replica in-cluster Redis (StatefulSet + PVC, defined in this
        stack). Without it these live in memory and are wiped on restart, so DCR
        clients get ``invalid_client`` and must re-register. Redis runs with
        requirepass; the CRD requires a password (aclUserConfig.passwordSecretRef).

The ``VirtualMCPServer`` is exposed to the internet through the shared APISIX gateway
on the operations cluster at ``toolhive-swe.ci.ol.mit.edu`` using the hybrid HTTPRoute
+ ApisixTls pattern (ADR-0003). The hostname is added to the operations EKS stack's
``eks:apisix_domains`` so external-dns points it at the APISIX NLB.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

stack_info = parse_stack()

# Stack config. The signing key, HMAC secret, and Redis password are stored as
# ENCRYPTED stack config (``pulumi config set --secret``) rather than generated in
# Pulumi state, matching the repo's practice for managed secrets. See the generation
# commands where each is consumed below, and the placeholders in Pulumi.<env>.yaml.
toolhive_swe_config = Config("toolhive_swe")

# K8s stack reference + provider for the operations cluster.
cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Reference the operator stack and eagerly require its output so this stack fails
# fast if the ToolHive operator and its CRDs have not been deployed yet — the
# MCPServer CustomResource below cannot be reconciled without them.
operator_stack = make_stack_reference(projects.TOOLHIVE_OPERATOR, stack_info.name)
require_stack_output_value(operator_stack, "toolhive_namespace")

TOOLHIVE_NAMESPACE = "toolhive-swe"

# The namespace is provisioned by the EKS operations stack; fail fast if missing.
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(TOOLHIVE_NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "toolhive",
        "Owner": "platform-engineering",
    }
)

# Typed labels for OL component resources (e.g. the Vault auth binding).
k8s_labels = K8sGlobalLabels(
    service=Services.toolhive,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
# Plain label dict applied to the raw K8s objects we manage directly.
k8s_global_labels = k8s_labels.model_dump()

# Name shared by the MCPGroup and every backend/virtual server that references it.
MCP_GROUP_NAME = "swe-tools"

# Public hostname the vMCP is served on. This is also the embedded auth server's
# issuer and the OAuth resource identifier ToolHive advertises + validates.
VMCP_DOMAIN = "toolhive-swe.ci.ol.mit.edu"
VMCP_RESOURCE_URL = f"https://{VMCP_DOMAIN}"
# RFC 8707 resource identifier / token audience. MCP clients (e.g. Claude Code)
# canonicalize a bare origin with a trailing slash per WHATWG URL rules and send
# THAT as the `resource` parameter, so the registered audience must include the
# trailing slash or the embedded auth server rejects the token request with
# "resource is not a registered audience". (The AS issuer, by contrast, must NOT
# have a trailing slash, so it keeps using VMCP_RESOURCE_URL.)
VMCP_RESOURCE_ID = f"{VMCP_RESOURCE_URL}/"
# Where Keycloak redirects after the user logs in (handled by the vMCP broker).
VMCP_OAUTH_CALLBACK = f"{VMCP_RESOURCE_URL}/oauth/callback"

# Keycloak realm the embedded auth server brokers login to (upstream OIDC provider).
# The SSO hostname follows the per-environment convention sso[-<env>].ol.mit.edu.
if stack_info.env_suffix == "production":
    KEYCLOAK_DOMAIN = "sso.ol.mit.edu"
else:
    KEYCLOAK_DOMAIN = f"sso-{stack_info.env_suffix}.ol.mit.edu"
KEYCLOAK_ISSUER = f"https://{KEYCLOAK_DOMAIN}/realms/ol-platform-engineering"
OIDC_CLIENT_ID = "ol-toolhive-client"
MCP_OIDC_CONFIG_NAME = "swe-vmcp-oidc"

# K8s Secret (synced from Vault by VSO) holding the Keycloak client secret that the
# embedded auth server uses to broker to the upstream provider.
UPSTREAM_SECRET_NAME = "toolhive-swe-oidc-upstream"  # noqa: S105  # pragma: allowlist secret
UPSTREAM_SECRET_KEY = "client-secret"  # noqa: S105  # pragma: allowlist secret

# Persistent signing material for the embedded auth server. Generated once and held
# in Pulumi state (KMS-encrypted via the stack's secretsprovider) so it is stable
# across deploys — mirroring how the vault stack generates its listener TLS key. This
# is what lets issued tokens survive vMCP pod restarts (vs. the ephemeral keys the
# auth server would otherwise generate on startup).
SIGNING_KEY_SECRET_NAME = "toolhive-swe-authserver-signing-key"  # noqa: S105  # pragma: allowlist secret
SIGNING_KEY_SECRET_KEY = "signing-key"  # noqa: S105  # pragma: allowlist secret
HMAC_SECRET_NAME = "toolhive-swe-authserver-hmac"  # noqa: S105  # pragma: allowlist secret
HMAC_SECRET_KEY = "hmac-key"  # noqa: S105  # pragma: allowlist secret

# In-cluster Redis backing the embedded auth server's persistent storage (OAuth
# sessions + DCR client registrations), so those survive vMCP pod restarts. A small
# single-replica StatefulSet with a PVC, local to this namespace. The CRD requires a
# password (aclUserConfig.passwordSecretRef) for the Redis backend, so Redis runs
# with requirepass and the vMCP references the same secret.
REDIS_SERVICE_NAME = "toolhive-swe-redis"
REDIS_ADDR = f"{REDIS_SERVICE_NAME}.{TOOLHIVE_NAMESPACE}.svc.cluster.local:6379"
REDIS_PASSWORD_SECRET_NAME = "toolhive-swe-redis-password"  # noqa: S105  # pragma: allowlist secret
REDIS_PASSWORD_SECRET_KEY = "password"  # noqa: S105  # pragma: allowlist secret
REDIS_IMAGE = "redis:7.4-alpine"
REDIS_STORAGE_CLASS = "ebs-gp3-sc"

#############################################
#   Vault auth binding (for VSO secret sync)#
#############################################
# No AWS access is required, so no IAM policy is attached (iam_policy_document=None).
# The binding provisions the Vault Secrets Operator wiring (VaultConnection /
# VaultAuth / sync service account) plus a Vault policy granting read access to the
# Keycloak client secret at secret-operations/sso/toolhive.
toolhive_swe_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="toolhive-swe",
        namespace=TOOLHIVE_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("toolhive_swe_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="toolhive-swe",
        vault_sync_service_account_names="toolhive-swe-vault",
        k8s_labels=k8s_labels,
    )
)

# Sync the Keycloak client secret from Vault into a namespace-local K8s Secret so
# ToolHive's embedded auth server can reference it as the upstream clientSecretRef.
upstream_oidc_secret = OLVaultK8SSecret(
    f"toolhive-swe-oidc-upstream-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=UPSTREAM_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=UPSTREAM_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/toolhive",
        exclude_raw=True,
        excludes=[".*"],
        templates={UPSTREAM_SECRET_KEY: '{{ get .Secrets "client_secret" }}'},
        refresh_after="1h",
        vaultauth=toolhive_swe_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=toolhive_swe_auth_binding.vault_k8s_resources,
    ),
)

##############################################
#   Embedded auth server persistent keys      #
##############################################
# Signing material from encrypted stack config so it is stable across deploys (and
# not regenerated), which keeps issued tokens valid across vMCP pod restarts.
# Generate + set (per environment):
#   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
#     | pulumi config set --secret toolhive_swe:auth_server_signing_key --
#   openssl rand -base64 32 \
#     | pulumi config set --secret toolhive_swe:auth_server_hmac_secret --
# RSA-2048 PKCS#8 PEM signing key used by the embedded auth server to sign JWTs.
authserver_signing_key_pem = toolhive_swe_config.require_secret(
    "auth_server_signing_key"
)
# 256-bit base64 HMAC secret.
authserver_hmac_secret_value = toolhive_swe_config.require_secret(
    "auth_server_hmac_secret"
)

# Materialise both as K8s Secrets referenced by authServerConfig below.
authserver_signing_key_secret = kubernetes.core.v1.Secret(
    f"toolhive-swe-authserver-signing-key-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=SIGNING_KEY_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    type="Opaque",
    string_data={SIGNING_KEY_SECRET_KEY: authserver_signing_key_pem},
    opts=ResourceOptions(delete_before_replace=True),
)
authserver_hmac_key_secret = kubernetes.core.v1.Secret(
    f"toolhive-swe-authserver-hmac-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=HMAC_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    type="Opaque",
    string_data={HMAC_SECRET_KEY: authserver_hmac_secret_value},
    opts=ResourceOptions(delete_before_replace=True),
)

##############################################
#   In-cluster Redis (embedded AS storage)    #
##############################################
# Redis requirepass from encrypted stack config. Generate + set (per environment):
#   openssl rand -base64 32 \
#     | pulumi config set --secret toolhive_swe:redis_password --
redis_password_value = toolhive_swe_config.require_secret("redis_password")
redis_password_secret = kubernetes.core.v1.Secret(
    f"toolhive-swe-redis-password-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=REDIS_PASSWORD_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    type="Opaque",
    string_data={REDIS_PASSWORD_SECRET_KEY: redis_password_value},
    opts=ResourceOptions(delete_before_replace=True),
)

# Headless Service: governs the StatefulSet and is the address the vMCP connects to.
redis_service = kubernetes.core.v1.Service(
    f"toolhive-swe-redis-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=REDIS_SERVICE_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        cluster_ip="None",
        selector={"app.kubernetes.io/name": REDIS_SERVICE_NAME},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="redis", port=6379, target_port=6379, protocol="TCP"
            )
        ],
    ),
)

# Single-replica Redis with a small persistent volume. AOF persistence is enabled so
# sessions/registrations survive Redis restarts too, and requirepass is fed from the
# generated Secret. ``$(REDIS_PASSWORD)`` is substituted by Kubernetes from the env.
redis_pod_labels = {
    **k8s_global_labels,
    "app.kubernetes.io/name": REDIS_SERVICE_NAME,
}
redis_statefulset = kubernetes.apps.v1.StatefulSet(
    f"toolhive-swe-redis-statefulset-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=REDIS_SERVICE_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=redis_pod_labels,
    ),
    spec=kubernetes.apps.v1.StatefulSetSpecArgs(
        service_name=REDIS_SERVICE_NAME,
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app.kubernetes.io/name": REDIS_SERVICE_NAME}
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=redis_pod_labels),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="redis",
                        image=cached_image_uri(REDIS_IMAGE),
                        args=[
                            "redis-server",
                            "--requirepass",
                            "$(REDIS_PASSWORD)",
                            "--appendonly",
                            "yes",
                            "--dir",
                            "/data",
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="REDIS_PASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name=REDIS_PASSWORD_SECRET_NAME,
                                        key=REDIS_PASSWORD_SECRET_KEY,
                                    )
                                ),
                            )
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(container_port=6379)
                        ],
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=6379
                            ),
                            initial_delay_seconds=5,
                            period_seconds=10,
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=6379
                            ),
                            initial_delay_seconds=15,
                            period_seconds=20,
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "50m", "memory": "64Mi"},
                            limits={"cpu": "200m", "memory": "256Mi"},
                        ),
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="data", mount_path="/data"
                            )
                        ],
                    )
                ],
            ),
        ),
        volume_claim_templates=[
            kubernetes.core.v1.PersistentVolumeClaimArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name="data", labels=k8s_global_labels
                ),
                spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
                    access_modes=["ReadWriteOnce"],
                    storage_class_name=REDIS_STORAGE_CLASS,
                    resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
                        requests={"storage": "100Gi"}
                    ),
                ),
            )
        ],
    ),
    opts=ResourceOptions(depends_on=[redis_password_secret]),
)

#########################################
#   MCPGroup (backend grouping)          #
#########################################
# Groups the SWE backend MCP servers so a VirtualMCPServer can aggregate them.
# Backends join by setting ``spec.groupRef.name`` to this group's name.
swe_mcpgroup = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-mcpgroup-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPGroup",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=MCP_GROUP_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "description": (
            "SWE agent-class MCP servers aggregated behind the swe VirtualMCPServer"
        ),
    },
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

#########################################
#   fetch MCPServer (example workload)   #
#########################################
# The reference fetch MCP server. The operator reconciles this into a proxy
# Deployment + Service (``mcp-fetch-proxy``) reachable in-cluster at
# ``http://mcp-fetch-proxy.toolhive-swe.svc.cluster.local:8080/mcp``. It joins the
# MCPGroup above so the VirtualMCPServer discovers it as a backend.
fetch_mcpserver = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-fetch-mcpserver-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="fetch",
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "image": "ghcr.io/stackloklabs/gofetch/server:1.0.5",
        "transport": "streamable-http",
        "proxyPort": 8080,
        "mcpPort": 8080,
        "groupRef": {"name": MCP_GROUP_NAME},
        # Fetch needs outbound network access to retrieve URLs. The "network"
        # builtin profile grants egress; tighten to an allow-list ConfigMap when
        # the set of reachable hosts is known.
        "permissionProfile": {
            "type": "builtin",
            "name": "network",
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
    },
    opts=ResourceOptions(depends_on=[swe_mcpgroup]),
)

#########################################
#   MCPOIDCConfig (incoming validation)  #
#########################################
# Validates the JWTs issued by the vMCP's own embedded auth server, so the issuer
# is the vMCP endpoint itself (NOT Keycloak — Keycloak is the upstream the embedded
# server brokers to). Referenced by the VirtualMCPServer's incomingAuth below.
mcp_oidc_config = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-mcp-oidc-config-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPOIDCConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=MCP_OIDC_CONFIG_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "type": "inline",
        "inline": {
            "issuer": VMCP_RESOURCE_URL,
        },
    },
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

#########################################
#   VirtualMCPServer aggregator          #
#########################################
# Aggregates every backend in the ``swe-tools`` group behind a single endpoint and
# fronts them with an embedded OAuth authorization server that brokers login to
# Keycloak. Tool-name collisions across backends are resolved by prefixing with the
# workload name.
swe_virtualmcpserver = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-vmcp-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="VirtualMCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="swe-vmcp",
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "groupRef": {"name": MCP_GROUP_NAME},
        # Embedded auth server: the vMCP is the OAuth provider MCP clients talk to,
        # brokering interactive login to Keycloak as an upstream OIDC provider.
        "authServerConfig": {
            "issuer": VMCP_RESOURCE_URL,
            # Scopes every DCR-registered client is allowed to request at
            # /oauth/authorize regardless of what it registered with. MCP clients
            # (e.g. Claude Code) auto-request ``offline_access`` for refresh tokens
            # once the AS advertises it, so it must be permitted here or the AS
            # rejects the authorization with ``invalid_scope``. Kept deliberately
            # narrow (openid + offline_access); every value must also be in the
            # upstream-derived scope set below or the auth server fails to start.
            "baselineClientScopes": ["openid", "offline_access"],
            # Persistent signing material so issued tokens survive pod restarts
            # (omitting these makes the auth server generate ephemeral keys).
            "signingKeySecretRefs": [
                {"name": SIGNING_KEY_SECRET_NAME, "key": SIGNING_KEY_SECRET_KEY}
            ],
            "hmacSecretRefs": [{"name": HMAC_SECRET_NAME, "key": HMAC_SECRET_KEY}],
            # Persistent storage for OAuth sessions + DCR client registrations, so
            # clients don't have to re-register/re-auth after a vMCP pod restart.
            "storage": {
                "type": "redis",
                "redis": {
                    "addr": REDIS_ADDR,
                    # Password-only AUTH (usernameSecretRef omitted) against the
                    # requirepass-protected in-cluster Redis.
                    "aclUserConfig": {
                        "passwordSecretRef": {
                            "name": REDIS_PASSWORD_SECRET_NAME,
                            "key": REDIS_PASSWORD_SECRET_KEY,
                        },
                    },
                },
            },
            "upstreamProviders": [
                {
                    "name": "keycloak",
                    "type": "oidc",
                    "oidcConfig": {
                        "issuerUrl": KEYCLOAK_ISSUER,
                        "clientId": OIDC_CLIENT_ID,
                        "clientSecretRef": {
                            "name": UPSTREAM_SECRET_NAME,
                            "key": UPSTREAM_SECRET_KEY,
                        },
                        "redirectUri": VMCP_OAUTH_CALLBACK,
                        # offline_access so ToolHive obtains a refresh token from
                        # Keycloak and so it appears in the upstream-derived scope
                        # set that baselineClientScopes is validated against.
                        "scopes": ["openid", "profile", "email", "offline_access"],
                    },
                }
            ],
        },
        # Validate the JWTs the embedded auth server issues.
        "incomingAuth": {
            "type": "oidc",
            "oidcConfigRef": {
                "name": MCP_OIDC_CONFIG_NAME,
                # Trailing-slash form: matches the RFC 8707 resource MCP clients
                # actually send (see VMCP_RESOURCE_ID).
                "audience": VMCP_RESOURCE_ID,
                "resourceUrl": VMCP_RESOURCE_ID,
            },
        },
        "serviceType": "ClusterIP",
        "config": {
            "aggregation": {
                "conflictResolution": "prefix",
                "conflictResolutionConfig": {"prefixFormat": "{workload}_"},
            },
        },
    },
    opts=ResourceOptions(
        depends_on=[
            swe_mcpgroup,
            fetch_mcpserver,
            mcp_oidc_config,
            upstream_oidc_secret,
            authserver_signing_key_secret,
            authserver_hmac_key_secret,
            redis_password_secret,
            redis_service,
            redis_statefulset,
        ]
    ),
)

#########################################
#   Internet exposure via APISIX         #
#########################################
# The VirtualMCPServer's Service, created by the operator when it reconciles the
# ``swe-vmcp`` CR above. It listens on port 4483 (not the backend proxy's 8080).
# APISIX only terminates TLS and proxies through to it — all auth (OAuth endpoints +
# token validation) happens inside the vMCP, so no plugins are attached.
VMCP_SERVICE_NAME = "vmcp-swe-vmcp"
VMCP_SERVICE_PORT = 4483
VMCP_TLS_SECRET_NAME = "toolhive-swe-vmcp-tls"  # noqa: S105  # pragma: allowlist secret

# Per-host TLS: cert-manager issues a Let's Encrypt cert into VMCP_TLS_SECRET_NAME
# and the paired ApisixTls resource binds it to the host on the APISIX gateway.
# The hostname must also be present in the operations EKS stack's
# ``eks:apisix_domains`` so external-dns points it at the APISIX NLB.
vmcp_cert = OLCertManagerCert(
    f"toolhive-swe-vmcp-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="toolhive-swe-vmcp",
        k8s_namespace=TOOLHIVE_NAMESPACE,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        dest_secret_name=VMCP_TLS_SECRET_NAME,
        dns_names=[VMCP_DOMAIN],
    ),
    opts=ResourceOptions(depends_on=[swe_virtualmcpserver]),
)

# Gateway API HTTPRoute attaching the host to the shared operations APISIX gateway
# and routing all paths (MCP + OAuth + /.well-known/*) to the vMCP Service. No
# plugins: authentication happens inside the vMCP.
vmcp_httproute = OLApisixHTTPRoute(
    f"toolhive-swe-vmcp-apisix-httproute-{stack_info.env_suffix}",
    route_configs=[
        OLApisixHTTPRouteConfig(
            route_name="vmcp",
            hosts=[VMCP_DOMAIN],
            paths=["/*"],
            backend_service_name=VMCP_SERVICE_NAME,
            # Numeric port: the component maps the name "http" to 8071, which is
            # wrong for this Service — pass the real port explicitly.
            backend_service_port=VMCP_SERVICE_PORT,
            plugins=[],
        ),
    ],
    k8s_namespace=TOOLHIVE_NAMESPACE,
    k8s_labels=k8s_global_labels,
    opts=ResourceOptions(depends_on=[swe_virtualmcpserver]),
)

export("toolhive_namespace", TOOLHIVE_NAMESPACE)
export("mcp_group_name", MCP_GROUP_NAME)
export("vmcp_domain", VMCP_DOMAIN)
export("vmcp_oauth_issuer", VMCP_RESOURCE_URL)
export("vmcp_upstream_issuer", KEYCLOAK_ISSUER)
