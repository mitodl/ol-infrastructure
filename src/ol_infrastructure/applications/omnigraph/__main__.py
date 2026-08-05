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

Keycloak realm membership is turned into per-user entries in that map by the
CronJob in ``token_sync.py``, in environments that set
``omnigraph:keycloak_url``. Enabling it moves ownership of the actor-tokens
Vault path from this program to that job — see the writer split below.

Store upkeep — nightly fragment compaction and weekly version GC — runs as two
further CronJobs (``maintenance.py``), against the S3 store directly rather
than through the server. See ``docs/omnigraph-store-maintenance-runbook.md``.

The *other* kind of upkeep — data and schema migrations, which do go through the
server — runs in the witan stack as ``svc-witan-admin``, the break-glass
principal whose token this stack provisions alongside ``svc-witan-ci``. See
``docs/witan-admin-break-glass-runbook.md``.

Follow-up work this stack does NOT cover (tracked separately):
    - **Container image.** The ``omnigraph``/``pulumi-omnigraph`` Concourse
      pipeline builds the image once, owns the ECR repository itself
      (idempotent create-if-missing), and promotes the same image unchanged
      through CI -> QA -> Production — see ``data_tier.py``'s module
      docstring. ``schema.pg`` (agent-kit repo,
      ``mcp/servers/witan/schema/schema.pg``) must be baked into the image at
      build time — this Pulumi program has no access to agent-kit's tree.
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
from ol_infrastructure.applications.omnigraph.maintenance import (
    DEFAULT_CLEANUP_OLDER_THAN,
    DEFAULT_CLEANUP_SCHEDULE,
    DEFAULT_OPTIMIZE_SCHEDULE,
)
from ol_infrastructure.applications.omnigraph.storage import validate_storage_prefix
from ol_infrastructure.applications.omnigraph.token_sync import (
    ACTOR_TOKENS_VAULT_PATH,
    DEFAULT_SYNC_SCHEDULE,
    SERVICE_TOKENS_VAULT_PATH,
    create_token_sync,
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

# Storage-root override for a storage-format migration. Unset (the steady
# state) puts the cluster's graphs at the bucket root. Set it and they live at
# `s3://ol-data-witan-<env>/<prefix>` instead — which is how
# docs/omnigraph-storage-format-upgrade-runbook.md repoints the cluster at
# graphs rebuilt under a new root while the old root stays intact as the
# rollback.
#
# A prefix inside the managed bucket, NOT a full URI: the bucket, its IAM
# policy and the IRSA grant are all keyed to the derived name, so a free-form
# URI could aim the cluster at storage nothing has granted access to. It is
# also what keeps backups and versioning covering the new root for free.
#
# Validated here rather than at the point of use because the failure this
# guards against is silent: `omnigraph cluster validate` accepts any storage
# string, including an empty one, so a malformed root is not caught downstream
# — it just builds the graphs somewhere nobody looks. See the runbook's
# "cluster validate does not catch an empty storage:" note.
STORAGE_PREFIX: str = validate_storage_prefix(omnigraph_config.get("storage_prefix"))

# Keycloak realm -> actor-token sync. Set `omnigraph:keycloak_url` for an
# environment to turn it on; leaving it unset keeps that environment on the
# SOPS-only behaviour, which is the right default until its `witan-token-sync`
# OIDC client exists (substructure/keycloak/ol_platform_engineering.py). The URL
# is the switch rather than a separate boolean because there is nothing this can
# do without it, and one setting cannot disagree with itself.
KEYCLOAK_URL = omnigraph_config.get("keycloak_url")
KEYCLOAK_REALM = omnigraph_config.get("keycloak_realm") or "ol-platform-engineering"
TOKEN_SYNC_SCHEDULE = (
    omnigraph_config.get("token_sync_schedule") or DEFAULT_SYNC_SCHEDULE
)
_TOKEN_SYNC_ENABLED = bool(KEYCLOAK_URL)

# Scheduled store maintenance (maintenance.py). Overridable per environment
# because the two things that set the right cadence — write volume and how much
# version history is worth keeping — differ between a CI store that is mostly
# reindex churn and a Production one backing real team memory. The defaults are
# sized for Production and are deliberately conservative; see maintenance.py
# for why retention is an age rather than a version count.
OPTIMIZE_SCHEDULE = (
    omnigraph_config.get("optimize_schedule") or DEFAULT_OPTIMIZE_SCHEDULE
)
CLEANUP_SCHEDULE = omnigraph_config.get("cleanup_schedule") or DEFAULT_CLEANUP_SCHEDULE
CLEANUP_OLDER_THAN = (
    omnigraph_config.get("cleanup_older_than") or DEFAULT_CLEANUP_OLDER_THAN
)

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

# The break-glass maintenance principal (agent-kit ADR-0005 path b, ADR-0002 D4
# as amended). Same shape as the CI token above — a raw single-actor token
# carried on its own Vault path so the witan stack's maintenance Jobs mount only
# that one value, plus the matching entry in the actor-tokens map so
# omnigraph-server will accept it. Both come from the one SOPS record, checked
# against each other below.
#
# Separate from svc-witan-ci because the in-cluster migration Job would
# otherwise keep authenticating as the code-graph pipeline — an identity with no
# legitimate access to the memory graph at all — and separate from
# svc-witan-service because the serving tier should not hold a credential that
# can rewrite memory rows. Cedar grants it `change` only on the memory graph,
# where every human user already has it; on the code and bridge graphs it is
# read + schema only (agent-kit mcp/servers/witan/policy/).
#
# Optional, unlike the CI token: environments whose SOPS file predates this are
# left exactly as they were, still running maintenance as svc-witan-ci, and no
# resource here or in the witan stack materializes. Adding the two keys and
# deploying is what switches an environment over. See
# docs/witan-admin-break-glass-runbook.md.
WITAN_ADMIN_TOKEN_VAULT_PATH = "secret-operations/witan/admin-token"  # noqa: S105  # pragma: allowlist secret
WITAN_ADMIN_TOKEN_VAULT_KEY = "token"  # noqa: S105  # pragma: allowlist secret
WITAN_ADMIN_ACTOR_ID = "svc-witan-admin"

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

    # Optional, but all-or-nothing. Half-configuring this is worse than not
    # configuring it: a token in the actor map with no `admin_token` record
    # leaves a credential omnigraph-server accepts that nothing can use, and an
    # `admin_token` with no map entry gives the witan stack's Jobs a token the
    # server 401s — a failure that surfaces as a migration Job crash-looping on
    # every deploy, at the point where it gates the MCPServer.
    _witan_admin_token = _witan_secrets_source.get("admin_token")
    _mapped_admin_token = _actor_tokens_map.get(WITAN_ADMIN_ACTOR_ID)

    # ABSENT AND BLANK ARE DIFFERENT THINGS, and every check below turns on
    # `is None` rather than truthiness because of it. A key that is simply not
    # in the file is an environment that has not opted in; a key present but
    # empty — or holding a stray space, which is *truthy* — is a mistake in a
    # hand-edited SOPS file, and the two must not resolve to the same behaviour.
    # Treated as absent, an empty `admin_token` silently leaves maintenance
    # running as svc-witan-ci while the operator believes they switched it over,
    # which is the exact failure the all-or-nothing rule below exists to catch;
    # treated as present, a whitespace token gets written to Vault and 401s
    # everything that reads it.
    for _label, _value in (
        ("admin_token", _witan_admin_token),
        (f"actor_tokens['{WITAN_ADMIN_ACTOR_ID}']", _mapped_admin_token),
    ):
        if _value is not None and not str(_value).strip():
            msg = (
                f"omnigraph/secrets.{stack_info.env_suffix}.yaml: {_label} is "
                "present but empty. Remove the key to leave this environment on "
                "svc-witan-ci, or give it a real token — an empty value is not "
                "the same as an absent one "
                "(see docs/witan-admin-break-glass-runbook.md)."
            )
            raise ValueError(msg)

    if (_witan_admin_token is None) != (_mapped_admin_token is None):
        msg = (
            f"omnigraph/secrets.{stack_info.env_suffix}.yaml: 'admin_token' and "
            f"actor_tokens['{WITAN_ADMIN_ACTOR_ID}'] must be set together or "
            "not at all — one without the other provisions a credential that "
            "cannot authenticate (see docs/witan-admin-break-glass-runbook.md)."
        )
        raise ValueError(msg)
    if _witan_admin_token is not None and _mapped_admin_token != _witan_admin_token:
        msg = (
            f"omnigraph/secrets.{stack_info.env_suffix}.yaml: admin_token must "
            f"match actor_tokens['{WITAN_ADMIN_ACTOR_ID}'] — they are the same "
            "token exposed to two different consumers (agent-kit ADR-0005 path b)."
        )
        raise ValueError(msg)
    if _witan_admin_token is not None and _witan_admin_token == _witan_ci_token:
        msg = (
            f"omnigraph/secrets.{stack_info.env_suffix}.yaml: admin_token must "
            "not equal ci_token. The whole point of the break-glass principal "
            "is that maintenance stops running as the code-graph pipeline; one "
            "shared value would make the two identities indistinguishable to "
            "the server while looking separate here."
        )
        raise ValueError(msg)
else:
    _actor_tokens_map = {}
    _witan_ci_token = None
    _witan_admin_token = None

# The non-human actors, on their own Vault path. This is always written, in
# every environment, and is the *input* the token-sync job merges Keycloak's
# per-user entries into — see WHO WRITES actor-tokens below.
service_tokens_vault_secret = None
if _actor_tokens_map:
    service_tokens_vault_secret = vault.generic.Secret(
        f"omnigraph-service-tokens-vault-secret-{stack_info.env_suffix}",
        path=SERVICE_TOKENS_VAULT_PATH,
        data_json=Output.secret(
            json.dumps({ACTOR_TOKENS_VAULT_KEY: json.dumps(_actor_tokens_map)})
        ),
    )

##############################################
#   WHO WRITES actor-tokens                   #
##############################################
# Exactly one writer, but which one depends on whether this environment has a
# Keycloak client provisioned for the sync job:
#
#   token sync OFF -> this program writes secret-operations/witan/actor-tokens
#                     straight from the SOPS map. There are no per-user entries
#                     to preserve, so the merged map and the service map are the
#                     same thing.
#   token sync ON  -> the CronJob in token_sync.py writes it, as
#                     service-tokens plus one act-<sub> entry per enabled
#                     human realm user, and this program stops writing it.
#
# The two must never overlap. A Pulumi write alongside the job's would revert
# every per-user entry on each `pulumi up` — every user 401ing until the next
# hourly run, plus an omnigraph-server restart at each end of that window.
#
# retain_on_delete is what makes the OFF -> ON transition safe. Removing this
# resource from the program would otherwise DELETE the Vault path, and the
# bootstrap Job that rewrites it is not ordered against that deletion; the
# window between them is one where omnigraph-server has no valid token for
# anybody. Retained, Pulumi drops it from state and leaves the content alone,
# and the job's first run takes over an already-populated path.
#
# WHICH MAKES THE ROLLOUT TWO STEPS, NOT ONE. Pulumi reads retainOnDelete from
# the STATE at deletion time, and a resource absent from the program is never
# re-registered — so the flag has to already be recorded before the environment
# is switched on. Deploy this change with the switch still off (one `pulumi up`
# that records the flag against an otherwise unchanged resource), and only then
# set `omnigraph:keycloak_url` and deploy again. Doing both in one `pulumi up`
# deletes the Vault path and leaves every user 401ing until the bootstrap Job
# lands. See docs/witan-token-sync-runbook.md.
actor_tokens_vault_secret = None
if _actor_tokens_map and not _TOKEN_SYNC_ENABLED:
    actor_tokens_vault_secret = vault.generic.Secret(
        f"omnigraph-actor-tokens-vault-secret-{stack_info.env_suffix}",
        path=ACTOR_TOKENS_VAULT_PATH,
        data_json=Output.secret(
            json.dumps({ACTOR_TOKENS_VAULT_KEY: json.dumps(_actor_tokens_map)})
        ),
        opts=ResourceOptions(retain_on_delete=True),
    )

if _witan_ci_token:
    vault.generic.Secret(
        f"omnigraph-witan-ci-token-vault-secret-{stack_info.env_suffix}",
        path="secret-operations/witan/ci-token",
        data_json=Output.secret(
            json.dumps({WITAN_CI_TOKEN_VAULT_KEY: _witan_ci_token})
        ),
    )

# The break-glass principal's own path, written the same way and by the same
# sole writer. Nothing in THIS stack consumes it — the maintenance Jobs that do
# live in the witan stack, which reads it through its own VSO sync (one writer
# per Vault path, as above).
if _witan_admin_token is not None:
    vault.generic.Secret(
        f"omnigraph-witan-admin-token-vault-secret-{stack_info.env_suffix}",
        path=WITAN_ADMIN_TOKEN_VAULT_PATH,
        data_json=Output.secret(
            json.dumps({WITAN_ADMIN_TOKEN_VAULT_KEY: _witan_admin_token})
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

##############################################
#   Keycloak realm -> actor tokens             #
##############################################
token_sync = None
if _TOKEN_SYNC_ENABLED:
    token_sync = create_token_sync(
        stack_info=stack_info,
        namespace=NAMESPACE,
        k8s_global_labels=k8s_global_labels,
        # Non-None by _TOKEN_SYNC_ENABLED, which is exactly `bool(KEYCLOAK_URL)`.
        keycloak_url=KEYCLOAK_URL or "",
        keycloak_realm=KEYCLOAK_REALM,
        vault_address=Config("vault").get("address")
        or f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_name=omnigraph_auth_binding.vault_k8s_resources.auth_name,
        schedule=TOKEN_SYNC_SCHEDULE,
        # The job reads the service map on every run and refuses to write an
        # actor map without it, so the Vault write has to land first.
        depends_on=[
            omnigraph_auth_binding.vault_k8s_resources,
            *([service_tokens_vault_secret] if service_tokens_vault_secret else []),
        ],
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
        # Whichever of the two writers this environment uses has to have
        # written the Vault path before the VSO is asked to render it —
        # otherwise the Secret comes up empty and the Deployment that mounts it
        # sits in ContainerCreating. Exactly one of these is ever non-None.
        depends_on=[
            omnigraph_auth_binding.vault_k8s_resources,
            *([actor_tokens_vault_secret] if actor_tokens_vault_secret else []),
            *([token_sync.bootstrap_job] if token_sync else []),
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
    optimize_schedule=OPTIMIZE_SCHEDULE,
    cleanup_schedule=CLEANUP_SCHEDULE,
    cleanup_older_than=CLEANUP_OLDER_THAN,
    storage_prefix=STORAGE_PREFIX,
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
# Mid-migration, the question an operator has is "which root is being served",
# so export the resolved URI — not just the config knob that shaped it, which
# is empty in the steady state and says nothing about the bucket. The prefix
# goes out alongside it because that is the value they would set or clear.
export("storage_uri", data_tier.storage_uri)
export("storage_prefix", STORAGE_PREFIX)
# Whether this environment's SOPS file carries the break-glass principal, and
# therefore whether secret-operations/witan/admin-token exists. A boolean, not
# the token: the witan stack needs to decide *whether* to declare its
# admin-token Secret and maintenance Jobs, which is a program-time branch, and
# it reads this eagerly (optional_stack_output_value) for exactly that reason.
# Publishing the value itself would put a live credential in this stack's
# outputs, where `pulumi stack output` and every consumer's state would carry
# it — the token travels through Vault, which is what Vault is for.
export("admin_token_provisioned", _witan_admin_token is not None)
