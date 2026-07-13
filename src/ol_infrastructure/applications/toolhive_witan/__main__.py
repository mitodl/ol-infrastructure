"""Deploy witan as a shared, multi-tenant MCP service on the operations cluster.

This stack owns the ``toolhive-witan`` namespace and implements
``docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md``: two
separate workloads on the existing shared ToolHive operator infrastructure.

- **MCP tier** — witan's own FastMCP process (``mcp_servers.py``), converted
  to ``streamable-http`` transport, registered as an ``MCPServer`` joined to
  the ``witan-tools`` ``MCPGroup``, aggregated behind a ``VirtualMCPServer``.
- **Data tier** — a separate stateless ``omnigraph-server`` Deployment
  (``data_tier.py``), S3-backed via an ``OLBucket`` + IRSA, reached by the MCP
  tier over the cluster network only.

Incoming auth — ToolHive's "External OIDC provider" scenario, NOT
``toolhive_swe``'s "Embedded auth server" scenario:

    ``toolhive_swe``'s vMCP is itself an OAuth provider: it brokers login to
    Keycloak upstream but then mints its **own** JWT (issuer == the vMCP's own
    URL) for its backends, none of which have any identity of their own — see
    ``toolhive_swe/__main__.py``'s module docstring. witan is different: its
    own FastMCP server independently validates a Keycloak-issued JWT and
    derives a per-request actor id from ``sub`` (agent-kit ADR-0004 D1/D2).
    Swapping that JWT for a vMCP-minted one before it reaches witan would
    break D1 outright. So this stack configures ``incomingAuth`` to validate
    directly against Keycloak's **real** issuer (no ``authServerConfig``,
    hence no embedded broker, no persistent signing keys, no Redis — none of
    which toolhive_swe's variant needs either, once there's no broker to keep
    state for) — ToolHive then has nothing of its own to substitute and simply
    forwards the client's original bearer token to the ``witan`` MCPServer.
    See ADR-0009's Resolution addendum and agent-kit ADR-0004's matching
    Resolution addendum (2026-07-10) for the full decision record.

    This also means clients need an already-valid Keycloak JWT with the right
    audience before calling — there is no vMCP-brokered interactive login
    here. That is intentional (agent-kit ADR-0004 D3: per-user omnigraph
    bearer tokens are pre-provisioned out-of-band, not minted on the fly), but
    it does mean whatever normally gets a human or CI agent a Keycloak JWT for
    other internal tools (existing SSO session, device-code flow, etc.) is a
    prerequisite this stack does not itself provide.

Follow-up work this stack does NOT cover, tracked separately rather than
silently assumed:
    - **Container images.** Neither witan nor omnigraph-server has a
      Dockerfile or CI image-build job in either repo yet (confirmed via
      repo-wide search of both at authoring time). This stack only creates
      the ECR repositories and references ``:latest``, following
      ``kubewatch_webhook_handler``'s "image built separately, by a Concourse
      job, before this stack runs" split — that Concourse job still needs to
      be written for both images before this stack can actually deploy
      anything live.
    - **Keycloak witan-users token provisioning.** agent-kit ADR-0004 D3
      assigns this stack the job of "walking the Keycloak witan-users
      group/role membership and writing a generated token per user" into the
      shared actor-tokens source. This stack only provisions the destination
      (the Vault-backed ``witan-actor-tokens`` Secret, seeded with at minimum
      the ``svc-witan-ci`` entry) — the sync job that keeps per-user entries
      current as Keycloak group membership changes is not yet built.
"""

import json
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from ol_infrastructure.applications.toolhive_witan.data_tier import (
    create_data_tier,
    omnigraph_server_addr,
)
from ol_infrastructure.applications.toolhive_witan.ingress import (
    create_ingress_resources,
)
from ol_infrastructure.applications.toolhive_witan.mcp_servers import (
    MCP_GROUP_NAME,
    create_mcp_servers,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
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
toolhive_witan_config = Config("toolhive_witan")

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Fail fast if the ToolHive operator and CRDs haven't been deployed yet.
operator_stack = make_stack_reference(projects.TOOLHIVE_OPERATOR, stack_info.name)
require_stack_output_value(operator_stack, "toolhive_namespace")

TOOLHIVE_NAMESPACE = "toolhive-witan"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(TOOLHIVE_NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "toolhive-witan",
        "Owner": "platform-engineering",
    }
)

k8s_labels = K8sGlobalLabels(
    service=Services.toolhive,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

# Public hostname the vMCP is served on, following the same per-environment
# convention as toolhive-swe.
if stack_info.env_suffix == "production":
    VMCP_DOMAIN = "toolhive-witan.ol.mit.edu"
else:
    VMCP_DOMAIN = f"toolhive-witan.{stack_info.env_suffix}.ol.mit.edu"
VMCP_RESOURCE_URL = f"https://{VMCP_DOMAIN}"
VMCP_RESOURCE_ID = f"{VMCP_RESOURCE_URL}/"

# Keycloak realm issuing the JWTs witan validates directly (ADR-0004 D1) —
# this is the REAL upstream issuer, unlike toolhive_swe's vMCP-local issuer,
# since there is no embedded broker minting a substitute token here.
if stack_info.env_suffix == "production":
    KEYCLOAK_DOMAIN = "sso.ol.mit.edu"
else:
    KEYCLOAK_DOMAIN = f"sso-{stack_info.env_suffix}.ol.mit.edu"
KEYCLOAK_ISSUER = f"https://{KEYCLOAK_DOMAIN}/realms/ol-platform-engineering"
MCP_OIDC_CONFIG_NAME = "witan-vmcp-oidc"

# The audience witan's own JWTVerifier validates (WITAN_OIDC_AUDIENCE,
# agent-kit ADR-0004 D1) and the vMCP's incomingAuth checks for. Configurable
# per stack in case the eventual Keycloak client/audience-mapper work lands a
# different value; defaults to a plain "witan" audience.
WITAN_OIDC_AUDIENCE = toolhive_witan_config.get("oidc_audience") or "witan"

# Vault-synced secrets: the svc-witan-ci raw token (ADR-0009 decision point 3)
# and the {actor_id: token} JSON map both witan (WITAN_ACTOR_TOKENS_FILE) and
# omnigraph-server (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE) read (agent-kit
# ADR-0004 D3). See the module docstring above re: the per-user sync job
# that keeps the latter current not yet existing.
WITAN_CI_TOKEN_SECRET_NAME = "witan-ci-token"  # noqa: S105  # pragma: allowlist secret
WITAN_CI_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_NAME = "witan-actor-tokens"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_KEY = "tokens.json"  # noqa: S105  # pragma: allowlist secret

##############################################
#   Vault auth binding (IRSA + VSO sync)      #
##############################################
# omnigraph-server is the only workload that needs AWS access (its S3-backed
# store), granted via IRSA below (data_tier.py attaches the actual bucket
# policy once the OLBucket ARN is known). Both witan and omnigraph-server
# need the Vault Secrets Operator sync wiring for the secrets above, hence
# both -vault service accounts in vault_sync_service_account_names.
witan_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="toolhive-witan",
        namespace=TOOLHIVE_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("toolhive_witan_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="omnigraph-server",
        create_irsa_service_account=True,
        vault_sync_service_account_names=["witan-vault", "omnigraph-server-vault"],
        k8s_labels=k8s_labels,
    )
)

witan_ci_token_secret = OLVaultK8SSecret(
    f"toolhive-witan-ci-token-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=WITAN_CI_TOKEN_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=WITAN_CI_TOKEN_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="witan/ci-token",
        exclude_raw=True,
        excludes=[".*"],
        templates={WITAN_CI_TOKEN_SECRET_KEY: '{{ get .Secrets "token" }}'},
        refresh_after="1h",
        vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=witan_auth_binding.vault_k8s_resources,
    ),
)

actor_tokens_secret = OLVaultK8SSecret(
    f"toolhive-witan-actor-tokens-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=ACTOR_TOKENS_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=ACTOR_TOKENS_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="witan/actor-tokens",
        exclude_raw=True,
        excludes=[".*"],
        templates={ACTOR_TOKENS_SECRET_KEY: '{{ get .Secrets "tokens_json" }}'},
        refresh_after="15m",
        vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=witan_auth_binding.vault_k8s_resources,
    ),
)

#########################################
#   Data tier (omnigraph-server)         #
#########################################
data_tier = create_data_tier(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    aws_config=aws_config,
    auth_binding=witan_auth_binding,
    actor_tokens_secret_name=ACTOR_TOKENS_SECRET_NAME,
)

#########################################
#   ECR repository for the witan image   #
#########################################
# Same "repo here, image built separately" split as omnigraph-server's own
# ECR repo in data_tier.py — see this module's docstring.
witan_ecr_repository = aws.ecr.Repository(
    f"toolhive-witan-witan-ecr-repository-{stack_info.env_suffix}",
    name=f"witan-{stack_info.env_suffix.lower()}",
    image_tag_mutability="MUTABLE",
    image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
        scan_on_push=True,
    ),
    force_delete=True,
    tags=aws_config.tags,
)
aws.ecr.LifecyclePolicy(
    f"toolhive-witan-witan-ecr-lifecycle-{stack_info.env_suffix}",
    repository=witan_ecr_repository.name,
    policy=json.dumps(
        {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": "Keep last 10 images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": 10,
                    },
                    "action": {"type": "expire"},
                }
            ]
        }
    ),
)
witan_image = witan_ecr_repository.repository_url.apply(lambda url: f"{url}:latest")

#########################################
#   MCPGroup + witan MCPServer           #
#########################################
mcp_servers = create_mcp_servers(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    cluster_stack=cluster_stack,
    witan_image=witan_image,
    omnigraph_server_addr=omnigraph_server_addr(TOOLHIVE_NAMESPACE),
    oidc_issuer=KEYCLOAK_ISSUER,
    oidc_audience=WITAN_OIDC_AUDIENCE,
    actor_tokens_secret_name=ACTOR_TOKENS_SECRET_NAME,
    witan_ci_token_secret_name=WITAN_CI_TOKEN_SECRET_NAME,
    witan_ci_token_secret_key=WITAN_CI_TOKEN_SECRET_KEY,
)

#########################################
#   MCPOIDCConfig (incoming validation)  #
#########################################
# Points at Keycloak's REAL issuer (not a vMCP-local one) — see module
# docstring for why this is the "External OIDC provider" scenario, not
# toolhive_swe's "Embedded auth server" one.
mcp_oidc_config = kubernetes.apiextensions.CustomResource(
    f"toolhive-witan-mcp-oidc-config-{stack_info.env_suffix}",
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
            "issuer": KEYCLOAK_ISSUER,
        },
    },
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

#########################################
#   VirtualMCPServer aggregator          #
#########################################
# No authServerConfig block: unlike toolhive_swe, this vMCP is NOT an OAuth
# provider of its own. incomingAuth validates the client's genuine Keycloak
# JWT directly and — because there is no embedded auth server to substitute
# a token — that same JWT reaches the witan MCPServer unmodified, which is
# exactly the "External OIDC provider" scenario ADR-0009's Resolution
# addendum specifies.
witan_virtualmcpserver = kubernetes.apiextensions.CustomResource(
    f"toolhive-witan-vmcp-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="VirtualMCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="witan-vmcp",
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "groupRef": {"name": MCP_GROUP_NAME},
        "incomingAuth": {
            "type": "oidc",
            "oidcConfigRef": {
                "name": MCP_OIDC_CONFIG_NAME,
                "audience": WITAN_OIDC_AUDIENCE,
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
            mcp_servers.group,
            *mcp_servers.servers,
            mcp_oidc_config,
            witan_ci_token_secret,
            actor_tokens_secret,
            data_tier.service,
        ]
    ),
)

#########################################
#   Internet exposure via APISIX         #
#########################################
vmcp_cert, vmcp_httproute = create_ingress_resources(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    vmcp_domain=VMCP_DOMAIN,
    witan_virtualmcpserver=witan_virtualmcpserver,
)

export("toolhive_namespace", TOOLHIVE_NAMESPACE)
export("mcp_group_name", MCP_GROUP_NAME)
export("vmcp_domain", VMCP_DOMAIN)
export("vmcp_oidc_issuer", KEYCLOAK_ISSUER)
export("witan_ecr_repository_url", witan_ecr_repository.repository_url)
export("omnigraph_server_addr", omnigraph_server_addr(TOOLHIVE_NAMESPACE))
