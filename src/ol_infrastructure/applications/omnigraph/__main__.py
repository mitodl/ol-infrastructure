"""Deploy omnigraph-server: the S3-backed graph service that backs witan.

``omnigraph-server`` (an external Rust binary,
https://github.com/ModernRelay/omnigraph — not vendored in either repo) is a
stateless service whose entire state lives in S3. This stack owns the
``omnigraph`` namespace and deploys a single ``Deployment`` + ClusterIP
``Service`` (``data_tier.py``), an ``OLBucket`` reached via IRSA, and the
generated ``cluster.yaml`` ConfigMap that keeps the bucket name/region and
graph list in lockstep with the Pulumi-managed bucket.

This is deliberately a standalone service, not part of the witan MCP stack:
witan (``applications/witan``) is one consumer of it, reaching this
Deployment over the cluster network via a ``StackReference`` to this stack's
``omnigraph_server_addr`` output. ToolHive is not involved here at all — it
only runs the witan MCP tier, which is an implementation detail of that stack,
not this one.

Access to the graph is gated by omnigraph-server's own bearer-token auth
(``OMNIGRAPH_SERVER_BEARER_TOKENS_FILE``), whose ``{actor_id: token}`` map is
the same artifact witan resolves per-user tokens from — synced here from Vault
into the ``actor-tokens`` Secret (agent-kit ADR-0004 D3). See
``docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md``.

Follow-up work this stack does NOT cover (tracked separately):
    - **Container image.** The ``omnigraph``/``pulumi-omnigraph`` Concourse
      pipeline builds the image once, owns the ECR repository itself
      (idempotent create-if-missing), and promotes the same image unchanged
      through CI -> QA -> Production — see ``data_tier.py``'s module
      docstring. ``schema.pg`` (agent-kit repo,
      ``mcp/servers/witan/schema/schema.pg``) must be baked into the image at
      build time — this Pulumi program has no access to agent-kit's tree.
    - **Keycloak witan-users token sync.** This stack writes the Vault
      ``secret-operations/witan/actor-tokens`` source (below) from a
      hand-authored SOPS file, and provisions the ``actor-tokens`` Secret
      destination, but not the job that keeps per-user entries current as
      Keycloak group membership changes — today the SOPS file only ever
      carries the one ``svc-witan-ci`` entry.
"""

import json
from pathlib import Path
from typing import Any

import pulumi_vault as vault
from pulumi import Output, ResourceOptions, export

from bridge.secrets import sops as _bridge_sops
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.omnigraph.data_tier import (
    create_data_tier,
    omnigraph_server_addr,
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
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

# Resolve the bridge secrets directory once at module level using the sops
# module's own __file__ — the same base path read_yaml_secrets uses internally.
_BRIDGE_SECRETS_DIR = Path(_bridge_sops.__file__).parent

setup_vault_provider()

stack_info = parse_stack()

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

NAMESPACE = "omnigraph"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "omnigraph",
        "Owner": "platform-engineering",
    }
)

k8s_labels = K8sGlobalLabels(
    service=Services.omnigraph,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

# {actor_id: token} JSON map omnigraph-server boots its bearer-token auth from
# (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE). The same artifact witan resolves
# per-user tokens from in its own namespace — both sync from the one Vault
# source (secret-operations/witan/actor-tokens), agent-kit ADR-0004 D3.
ACTOR_TOKENS_SECRET_NAME = "actor-tokens"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_KEY = "tokens.json"  # noqa: S105  # pragma: allowlist secret
# Key the map is stored under *inside* the Vault secret. Populated below from a
# SOPS-encrypted, per-environment source file — this is the contract that file
# must follow (an "actor_tokens" mapping under this key), and the VSO template
# further down resolves to an empty Secret — and the app to an empty token
# map — if the Vault secret uses any other key.
ACTOR_TOKENS_VAULT_KEY = "tokens_json"  # pragma: allowlist secret
# witan's own module-level fallback OmnigraphClient authenticates as this raw
# token (WITAN_MEMORY_TOKEN) when a request carries no per-actor JWT — see
# applications/witan/__main__.py and witan_policy.hcl. Its value must match
# the "svc-witan-ci" entry of the actor-tokens map above; both come from the
# same SOPS source record below so they can't drift.
WITAN_CI_TOKEN_VAULT_KEY = "token"  # noqa: S105  # pragma: allowlist secret

##############################################
#   Vault secret source (SOPS -> Vault)       #
##############################################
# This stack is the sole writer of both Vault paths below — the witan stack
# (applications/witan) only ever reads them via its own OLVaultK8SSecret. One
# writer per path avoids two independent Pulumi programs racing to write the
# same Vault secret. The destination path is deliberately the same literal
# string in every environment: CI/QA/Production each already have their own
# physically separate Vault server (vault-<env>.odl.mit.edu, see
# setup_vault_provider), so environment separation comes from which Vault
# this stack's provider is pointed at, not from anything in the path itself.
# Not yet seeded for every environment (bootstrapping is manual), so this
# reads best-effort: an environment without the file yet just gets neither
# Vault write, leaving actor_tokens_secret's VSO sync exactly as unfulfilled
# as it is today rather than hard-failing the rest of this stack's resources.
_witan_secrets_path = Path(f"omnigraph/secrets.{stack_info.env_suffix}.yaml")
_witan_secrets_source: dict[str, Any] = {}
if (_BRIDGE_SECRETS_DIR / _witan_secrets_path).exists():
    _witan_secrets_source = read_yaml_secrets(_witan_secrets_path)

_actor_tokens_map = _witan_secrets_source.get("actor_tokens") or {}
_witan_ci_token = _witan_secrets_source.get("ci_token")
if _witan_ci_token and _actor_tokens_map.get("svc-witan-ci") != _witan_ci_token:
    msg = (
        f"omnigraph/secrets.{stack_info.env_suffix}.yaml: ci_token must match "
        "actor_tokens['svc-witan-ci'] (ADR-0009 decision point 3) — they are "
        "the same token exposed to two different consumers."
    )
    raise ValueError(msg)

actor_tokens_vault_secret = None
if _actor_tokens_map:
    actor_tokens_vault_secret = vault.generic.Secret(
        f"omnigraph-actor-tokens-vault-secret-{stack_info.env_suffix}",
        path="secret-operations/witan/actor-tokens",
        data_json=Output.secret(
            json.dumps({ACTOR_TOKENS_VAULT_KEY: json.dumps(_actor_tokens_map)})
        ),
    )

if _witan_ci_token:
    vault.generic.Secret(
        f"omnigraph-witan-ci-token-vault-secret-{stack_info.env_suffix}",
        path="secret-operations/witan/ci-token",
        data_json=Output.secret(
            json.dumps({WITAN_CI_TOKEN_VAULT_KEY: _witan_ci_token})
        ),
    )

##############################################
#   Vault auth binding (IRSA + VSO sync)      #
##############################################
# omnigraph-server needs AWS access for its S3-backed store (IRSA below;
# data_tier.py attaches the bucket policy once the OLBucket ARN is known) plus
# the Vault Secrets Operator sync wiring for the actor-tokens Secret.
omnigraph_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="omnigraph",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("omnigraph_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="omnigraph-server",
        create_irsa_service_account=True,
        # Must match the ServiceAccount OLVaultK8SResources creates for the VSO
        # to authenticate with, which is always f"{application_name}-vault".
        vault_sync_service_account_names=["omnigraph-vault"],
        k8s_labels=k8s_labels,
    )
)

actor_tokens_secret = OLVaultK8SSecret(
    f"omnigraph-actor-tokens-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=ACTOR_TOKENS_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=ACTOR_TOKENS_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="witan/actor-tokens",
        exclude_raw=True,
        excludes=[".*"],
        templates={
            ACTOR_TOKENS_SECRET_KEY: (
                f'{{{{ get .Secrets "{ACTOR_TOKENS_VAULT_KEY}" }}}}'
            )
        },
        refresh_after="15m",
        vaultauth=omnigraph_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            omnigraph_auth_binding.vault_k8s_resources,
            *([actor_tokens_vault_secret] if actor_tokens_vault_secret else []),
        ],
    ),
)

#########################################
#   omnigraph-server data tier           #
#########################################
data_tier = create_data_tier(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    aws_config=aws_config,
    auth_binding=omnigraph_auth_binding,
    actor_tokens_secret_name=ACTOR_TOKENS_SECRET_NAME,
    actor_tokens_secret=actor_tokens_secret,
)

export("namespace", NAMESPACE)
export("omnigraph_server_addr", omnigraph_server_addr(NAMESPACE))
export("omnigraph_server_image_repository", data_tier.image_repository)
