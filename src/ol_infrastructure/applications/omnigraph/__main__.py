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
      carries the one ``svc-witan-ci`` entry. What this stack DOES now cover
      is the other half of "add a user": the ``actor-tokens`` secret carries
      ``restart_targets`` so the Vault Secrets Operator bounces
      omnigraph-server whenever the token map changes (it only reads the map
      at boot). Whatever eventually writes that Vault path — the sync job, a
      break-glass ``vault kv put``, or this stack — gets the restart for free
      and does not need to orchestrate one itself.
"""

import json
from pathlib import Path
from typing import Any

import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, export

from bridge.secrets import sops as _bridge_sops
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.omnigraph.data_tier import (
    COUNCIL_GRAPH_ID,
    OMNIGRAPH_SERVER_SERVICE_NAME,
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
    OLVaultRestartTarget,
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
omnigraph_config = Config("omnigraph")

# Repos that get a per-repo `code-<repo>` graph on this cluster. Canonical repo
# URIs — the exact strings witan-code detects from a checkout's git remote and
# runs through witan_code.config.graph_id to pick its `--graph`; data_tier's
# code_graph_id mirrors that function to declare the same id here.
#
# Provisioning a new repo is deliberately explicit: add it here, `pulumi up`,
# then `omnigraph cluster apply` + restart the server (see data_tier's
# build_cluster_graphs). A repo that is not listed fails to resolve rather than
# silently minting a graph nobody provisioned or backs up.
MANAGED_REPOS: list[str] = omnigraph_config.get_object("managed_repos") or []

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
# Not yet seeded for every environment (bootstrapping is manual), so a
# *missing* file is read best-effort — that environment just gets neither
# Vault write, leaving actor_tokens_secret's VSO sync exactly as unfulfilled
# as it is today rather than hard-failing the rest of this stack's resources
# (same tolerance open_metadata/__main__.py uses for its own connector
# secrets). A *present* file is not optional, though: a decrypt failure or
# missing/mismatched keys fails the whole preview/up rather than silently
# creating actor_tokens_secret and the Deployment that mounts it against an
# incomplete or absent Vault source — which would just recreate the
# ContainerCreating bug this stack exists to fix, with `pulumi up` reporting
# success.
_witan_secrets_path = Path(f"omnigraph/secrets.{stack_info.env_suffix}.yaml")
_witan_secrets_source: dict[str, Any] = {}
if (_BRIDGE_SECRETS_DIR / _witan_secrets_path).exists():
    _witan_secrets_source = read_yaml_secrets(_witan_secrets_path)
    if not isinstance(_witan_secrets_source, dict):
        msg = (
            f"Failed to decrypt omnigraph/secrets.{stack_info.env_suffix}.yaml: "
            f"expected a dict but got {type(_witan_secrets_source).__name__}. "
            "Check that sops can decrypt the file and that Vault-transit/KMS "
            "access is available."
        )
        raise TypeError(msg)

    _actor_tokens_map = _witan_secrets_source.get("actor_tokens") or {}
    _witan_ci_token = _witan_secrets_source.get("ci_token")
    if not _witan_ci_token or not _actor_tokens_map:
        msg = (
            f"omnigraph/secrets.{stack_info.env_suffix}.yaml is missing "
            "required keys: both 'ci_token' and a non-empty 'actor_tokens' "
            "map are required once the file exists."
        )
        raise ValueError(msg)
    if _actor_tokens_map.get("svc-witan-ci") != _witan_ci_token:
        msg = (
            f"omnigraph/secrets.{stack_info.env_suffix}.yaml: ci_token must "
            "match actor_tokens['svc-witan-ci'] (ADR-0009 decision point 3) "
            "— they are the same token exposed to two different consumers."
        )
        raise ValueError(msg)
else:
    _actor_tokens_map = {}
    _witan_ci_token = None

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
        # omnigraph-server SHA-256-hashes the token map once at boot and never
        # re-reads the file (upstream docs/user/operations/server.md "Auth
        # model"; there is no SIGHUP, admin endpoint, or runtime registration
        # — see the upstream ask filed alongside this). So syncing a new token
        # into the Secret is only half of "add a user": without a restart the
        # server keeps 401-ing that bearer token indefinitely, even though
        # witan's own ActorTokenResolver — reading the SAME file — picks it up
        # on the very next request. The two halves have to be one operation.
        #
        # Making that one operation the VSO's job, rather than the (not yet
        # built) Keycloak token-sync job's, is deliberate:
        #   - it holds for EVERY writer of the Vault path, not just that job —
        #     this stack's own SOPS-sourced write, a break-glass `vault kv
        #     put`, and the sync job alike;
        #   - the sync job would otherwise need RBAC to patch a Deployment in
        #     a namespace it has no other business in, and "write Vault" and
        #     "restart the server" would be two independently-failing steps
        #     with a silently-stale server as the failure mode;
        #   - the VSO already owns "Vault content changed -> Secret updated",
        #     so it is the one controller that can observe the change.
        # A restart fires only when the rendered Secret content actually
        # changes, not on every `refresh_after` poll, so steady state is
        # quiet; 15m is the resulting worst-case onboarding latency.
        #
        # BLAST RADIUS, deliberately accepted: the data tier is replicas=1 +
        # strategy=Recreate (see data_tier.py — its storage is
        # strict-single-version, so two binaries must never hold the same S3
        # store), which makes this a hard ~10-30s outage of the graph, not a
        # rolling one. Every witan MCP call in that window would fail, so it
        # is paired with connect-failure retry in the client that absorbs the
        # gap (agent-kit packages/witan-core/witan_core/omnigraph.py,
        # _UNAVAILABLE_MARKERS: a ~42s budget, connection-ESTABLISHMENT
        # failures only). Do not shorten refresh_after below the point where
        # two restarts could overlap that retry budget.
        restart_targets=[
            OLVaultRestartTarget(kind="Deployment", name=OMNIGRAPH_SERVER_SERVICE_NAME)
        ],
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
    managed_repos=MANAGED_REPOS,
)

export("namespace", NAMESPACE)
export("omnigraph_server_addr", omnigraph_server_addr(NAMESPACE))
# The Layer-1 graph id consumers must address (`--graph <id>`). Exported
# rather than left to each consumer's own default so the witan stack asks
# for exactly the graph declared in cluster.yaml here.
export("council_graph_id", COUNCIL_GRAPH_ID)
export("omnigraph_server_image_repository", data_tier.image_repository)
# The repos whose `code-<repo>` graphs this cluster serves. Exported so the
# witan stack's CI indexer sweeps exactly the set of graphs declared here: the
# writer's repo list and the cluster's graph list are the same list, and a
# second copy of it in another stack's config could only ever drift.
export("managed_repos", MANAGED_REPOS)
