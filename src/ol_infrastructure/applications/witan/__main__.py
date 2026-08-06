"""Deploy witan as a shared, multi-tenant MCP service on the operations cluster.

This stack owns the ``witan`` namespace and implements the MCP tier of
``docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md``: witan's
own FastMCP process (``mcp_servers.py``), run over ``streamable-http``
transport, registered as an ``MCPServer`` joined to the ``witan-tools``
``MCPGroup`` and aggregated behind a ``VirtualMCPServer`` exposed through
APISIX.

The data tier — the ``omnigraph-server`` graph service witan reads/writes over
the cluster network — is a **separate stack** (``applications/omnigraph``),
reached here via a ``StackReference`` to its ``omnigraph_server_addr`` output.

Migrations are split along that same boundary. **Schema** convergence belongs
to the omnigraph stack, which runs ``omnigraph cluster apply`` before its
server restarts — it declares the graphs and bakes their schema files into the
omnigraph-server image. This stack runs only witan's own **data** backfills
(``migrations.py``), gated ahead of the MCPServer so a new image never serves
against a graph its migrations haven't run over. Both those backfills and the
ad-hoc maintenance operations the MCP path refuses (``break_glass.py``)
authenticate as ``svc-witan-admin`` where the omnigraph stack has provisioned it —
see ``docs/witan-admin-break-glass-runbook.md``.
ToolHive is only the operator that runs this MCP tier; it is an implementation
detail of this stack, not part of witan's or omnigraph's identity — hence the
plain ``witan`` / ``omnigraph`` project and namespace names.

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
    hence no embedded broker, no persistent signing keys, no Redis) — ToolHive
    then has nothing of its own to substitute and simply forwards the client's
    original bearer token to the ``witan`` MCPServer. See ADR-0009's
    Resolution addendum and agent-kit ADR-0004's matching Resolution addendum
    (2026-07-10) for the full decision record.

    This also means clients need an already-valid Keycloak JWT with the right
    audience before calling — there is no vMCP-brokered interactive login
    here. That is intentional (agent-kit ADR-0004 D3: per-user omnigraph
    bearer tokens are pre-provisioned out-of-band, not minted on the fly), but
    it does mean whatever normally gets a human or CI agent a Keycloak JWT for
    other internal tools (existing SSO session, device-code flow, etc.) is a
    prerequisite this stack does not itself provide.

Follow-up work this stack does NOT cover, tracked separately rather than
silently assumed:
    - **Container image.** The ``witan``/``pulumi-witan`` Concourse pipeline
      builds the image once and promotes it unchanged through CI -> QA ->
      Production. It also owns the ``witan`` ECR repository itself
      (idempotent create-if-missing on every build), rather than this stack
      creating it -- a single repo can't be owned by three independent
      per-env Pulumi stacks. This stack instead pins the Deployment's image
      by digest, read from ``WITAN_DOCKER_SHA`` (set by the build job via
      the pulumi-provisioner's ``env_vars_from_files``), so a new push always
      changes the pod spec and triggers a rollout instead of silently
      leaving the running pod on a stale image.
    - **Per-user token provisioning.** agent-kit ADR-0004 D3's "writing a
      generated token per user" into the shared actor-tokens source now
      exists, but it lives in the omnigraph stack next to the Vault path it
      writes — ``applications/omnigraph/token_sync.py``, a CronJob that
      enumerates the Keycloak realm's users. This stack only provisions the
      *destination*: the Vault-backed ``actor-tokens`` Secret its own
      containers mount. It is off until an environment sets
      ``omnigraph:keycloak_url``, and until then the omnigraph stack writes
      that Vault path from a SOPS file carrying only ``svc-witan-ci``.
    - **GitHub App registration.** The CI indexer can clone private repos as a
      GitHub App installation (``ci_indexer.py``). Registering that App on the
      org, granting it ``contents: read``, and installing it on the repos it
      may read are org-admin steps done by hand; the resulting id, installation
      id, and one-time-downloadable private key then go into
      ``src/bridge/secrets/witan/secrets.<env>.yaml`` (SOPS), which this stack
      reads and writes to Vault. Until that file exists the indexer clones
      anonymously — correct while every managed repo is public.
"""

import json
from pathlib import Path
from typing import Any

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, export

from bridge.secrets import sops as _bridge_sops
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.witan.break_glass import (
    BREAK_GLASS_CRONJOB_NAME,
    create_break_glass_cronjob,
)
from ol_infrastructure.applications.witan.ci_indexer import (
    DEFAULT_INDEX_SCHEDULE,
    create_ci_indexer,
)
from ol_infrastructure.applications.witan.ingress import create_ingress_resources
from ol_infrastructure.applications.witan.mcp_servers import (
    MCP_GROUP_NAME,
    create_mcp_servers,
)
from ol_infrastructure.applications.witan.migrations import create_migration_job
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
    format_docker_image_ref,
    make_stack_reference,
    optional_stack_output_value,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

# Resolve the bridge secrets directory once from the sops module's own
# __file__ — the same base path read_yaml_secrets uses internally.
_BRIDGE_SECRETS_DIR = Path(_bridge_sops.__file__).parent

setup_vault_provider()

stack_info = parse_stack()
witan_config = Config("witan")

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Fail fast if the ToolHive operator and CRDs haven't been deployed yet.
operator_stack = make_stack_reference(projects.TOOLHIVE_OPERATOR, stack_info.name)
require_stack_output_value(operator_stack, "toolhive_namespace")

# Fail fast if the omnigraph data-tier stack hasn't been deployed yet — witan's
# MCPServer points both WITAN_MEMORY_URI (the `council` graph) and
# WITAN_CODE_SERVER (the per-repo `code-<repo>` graphs) at its in-cluster
# address (below).
omnigraph_stack = make_stack_reference(projects.OMNIGRAPH, stack_info.name)
omnigraph_server_addr = require_stack_output_value(
    omnigraph_stack, "omnigraph_server_addr"
)
# The graph id witan addresses on that server (`--graph <id>`), taken from the
# stack that declares it in cluster.yaml rather than defaulted independently
# here — see WITAN_MEMORY_GRAPH in mcp_servers.py.
council_graph_id = require_stack_output_value(omnigraph_stack, "council_graph_id")
# The repos with a `code-<repo>` graph on that server. The CI indexer below is
# the single entitled writer of each of their default views, so it sweeps the
# list the cluster declares rather than one of this stack's own — see
# ci_indexer.py. Resolved eagerly (not via `.apply`) because it decides whether
# the CronJob is declared at all.
#
# Optional, unlike the two outputs above: this output is newer than the
# omnigraph stack's last deploy in every environment, and the two stacks are
# separate Concourse pipelines with no ordering between them, so requiring it
# would block every witan deploy until omnigraph happens to run. Absent means
# the same thing an empty list means — no code graphs to index yet, so no
# indexer — and the next omnigraph deploy supplies it.
managed_repos = optional_stack_output_value(omnigraph_stack, "managed_repos") or []
# Whether the omnigraph stack provisioned the break-glass principal for this
# environment — i.e. whether secret-operations/witan/admin-token exists (see that
# stack's WITAN_ADMIN_TOKEN_VAULT_PATH). A boolean, not the token: this stack
# reads the token itself through VSO, from Vault, like every other credential it
# holds.
#
# Optional and eagerly resolved for the same two reasons `managed_repos` is: it
# gates whether resources are declared at all (a program-time branch, not
# something `.apply` can express), and the two stacks deploy from independent
# pipelines, so requiring it would wedge every witan deploy until omnigraph
# happens to run. False means "not provisioned yet", which is the state every
# environment starts in — maintenance keeps running as svc-witan-ci until the
# SOPS keys are added and the omnigraph stack redeployed.
admin_token_provisioned = bool(
    optional_stack_output_value(
        omnigraph_stack, "admin_token_provisioned", default=False
    )
)
# Same shape, for svc-witan: whether the MCP tier has an account of its own to
# enumerate graphs as, or is still borrowing svc-witan-ci for it. False only in
# an environment whose omnigraph stack has no SOPS secrets file at all — where
# there is no service-token path to read, so falling back is the only option
# that yields a working Secret.
service_token_provisioned = bool(
    optional_stack_output_value(
        omnigraph_stack, "service_token_provisioned", default=False
    )
)

NAMESPACE = "witan"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "witan",
        "Owner": "platform-engineering",
    }
)

k8s_labels = K8sGlobalLabels(
    service=Services.witan,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

# Public hostname the vMCP is served on.
if stack_info.env_suffix == "production":
    VMCP_DOMAIN = "witan.ol.mit.edu"
else:
    VMCP_DOMAIN = f"witan.{stack_info.env_suffix}.ol.mit.edu"
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
WITAN_OIDC_AUDIENCE = witan_config.get("oidc_audience") or "witan"

# How often the CI indexer sweeps every managed repo's default branch onto its
# shared code graph. Per-stack so a lower environment can be turned down (or
# up, to shake the job out) without touching the default — see ci_indexer.py
# for why the interval is what bounds staleness of the shared view.
WITAN_CI_INDEX_SCHEDULE = (
    witan_config.get("ci_index_schedule") or DEFAULT_INDEX_SCHEDULE
)

# The GitHub App the CI indexer clones as, when one is configured. All three
# values come from one SOPS file rather than splitting the ids into plain
# Pulumi config: they are obtained together, in one sitting, when the App is
# registered, and a split invites setting the ids while forgetting the key —
# which mounts a Secret that never fulfills. This mirrors the shared
# release_bot App, whose id/installation-id/PEM likewise sit together in
# `concourse/operations.<env>.yaml`.
#
# A dedicated App, NOT that shared one (`shared/github_app`): release_bot is
# Concourse's and carries the issue/PR write it needs, where this needs only
# `contents: read`. Its own App also makes the *installation* the list of repos
# the indexer can read, set by an org admin rather than by this repo — so a
# private repo cannot enter a shared graph on a Pulumi change alone.
GITHUB_APP_SECRET_NAME = "witan-github-app"  # noqa: S105  # pragma: allowlist secret
GITHUB_APP_SECRET_KEY = "private-key.pem"  # noqa: S105  # pragma: allowlist secret
GITHUB_APP_VAULT_KEY = "private_key"  # pragma: allowlist secret

##############################################
#   GitHub App source (SOPS -> Vault)         #
##############################################
# Same tolerance the omnigraph stack applies to its own SOPS source, and for
# the same reason: an environment with no App registered yet has no file, and
# that must not fail the rest of the stack — the indexer simply clones
# anonymously, which is correct while every managed repo is public.
#
# A file that IS present, though, is not optional. A decrypt failure or a
# missing key fails the whole preview rather than quietly provisioning a
# CronJob that mounts a Secret Vault will never fulfill, whose only symptom
# would be every private repo failing to clone once every four hours.
_github_app_path = Path(f"witan/secrets.{stack_info.env_suffix}.yaml")
_github_app_source: dict[str, Any] = {}
if (_BRIDGE_SECRETS_DIR / _github_app_path).exists():
    _github_app_source = read_yaml_secrets(_github_app_path)
    if not isinstance(_github_app_source, dict):
        msg = (
            f"Failed to decrypt witan/secrets.{stack_info.env_suffix}.yaml: "
            f"expected a dict but got {type(_github_app_source).__name__}. "
            "Check that sops can decrypt the file and that Vault-transit/KMS "
            "access is available."
        )
        raise TypeError(msg)

    _missing = [
        key
        for key in ("app_id", "installation_id", "private_key")
        if not _github_app_source.get(key)
    ]
    if _missing:
        msg = (
            f"witan/secrets.{stack_info.env_suffix}.yaml is missing required "
            f"keys: {', '.join(_missing)}. All of app_id, installation_id and "
            "private_key are required once the file exists — a partial set "
            "would clone anonymously and fail on exactly the private repos the "
            "App was added for."
        )
        raise ValueError(msg)

# Ids are read as strings: a bare numeric App id in YAML parses as an int, and
# both the JWT `iss` claim and the installation URL want it as text.
WITAN_GITHUB_APP_ID = str(_github_app_source["app_id"]) if _github_app_source else None
WITAN_GITHUB_APP_INSTALLATION_ID = (
    str(_github_app_source["installation_id"]) if _github_app_source else None
)

# This stack is the sole writer of this Vault path — unlike the two below,
# which the omnigraph stack owns. No race: nothing else reads or writes it.
github_app_vault_secret = None
if _github_app_source:
    github_app_vault_secret = vault.generic.Secret(
        f"witan-github-app-vault-secret-{stack_info.env_suffix}",
        path="secret-operations/witan/github-app",
        data_json=Output.secret(
            json.dumps({GITHUB_APP_VAULT_KEY: _github_app_source["private_key"]})
        ),
    )

# Vault-synced secrets: the svc-witan-ci raw token (ADR-0009 decision point 3)
# and the {actor_id: token} JSON map witan reads (WITAN_ACTOR_TOKENS_FILE,
# agent-kit ADR-0004 D3) — omnigraph-server reads the same map from the same
# Vault source in its own namespace (see applications/omnigraph).
WITAN_CI_TOKEN_SECRET_NAME = "witan-ci-token"  # noqa: S105  # pragma: allowlist secret
WITAN_CI_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret
# The MCP tier's own credential against the code graphs (WITAN_CODE_TOKEN, see
# mcp_servers.py). A SEPARATE Secret holding the same Vault value rather than a
# second `spec.secrets` entry against witan-ci-token: that list is keyed by
# secret name, so two entries naming one Secret are rejected outright
# (`.spec.secrets: duplicate entries for key [name="witan-ci-token"]`).
#
# Which is the right shape anyway — these are two identities that happen to
# share a token today. witan's Cedar bundle models a distinct
# `witan-service`/`act-svc-witan` account for the tier's graph enumeration, and
# when one is provisioned this Secret's `path` moves to it while the MCPServer
# spec stays as it is.
WITAN_CODE_TOKEN_SECRET_NAME = (  # pragma: allowlist secret
    "witan-code-token"  # noqa: S105
)
WITAN_CODE_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_NAME = "actor-tokens"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_KEY = "tokens.json"  # noqa: S105  # pragma: allowlist secret
# Keys these maps are stored under *inside* their Vault secrets. This stack
# only reads them (OLVaultK8SSecret/VSO below) — the omnigraph stack
# (applications/omnigraph/__main__.py) is the sole writer, populating both
# from a SOPS-encrypted per-environment file. The VSO templates below resolve
# to empty Secrets — and the apps to empty tokens — if the Vault secrets use
# any other key, so the names are part of the contract.
WITAN_CI_TOKEN_VAULT_KEY = "token"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_VAULT_KEY = "tokens_json"  # pragma: allowlist secret

# The break-glass maintenance principal (agent-kit ADR-0005 path b / ADR-0002 D4
# as amended). Written to Vault by the omnigraph stack alongside the CI token;
# read here because the two things that use it — the pre-deploy migration Job and
# the break-glass pod template — both live in this namespace.
#
# Its own Secret rather than a key in the actor-tokens map, even though the map
# contains the same value: a Job mounts one env var, and mounting the whole map
# into a maintenance pod would hand it every user's token as well.
WITAN_ADMIN_TOKEN_SECRET_NAME = (  # pragma: allowlist secret
    "witan-admin-token"  # noqa: S105
)
WITAN_ADMIN_TOKEN_SECRET_KEY = "token"  # noqa: S105  # pragma: allowlist secret
WITAN_ADMIN_TOKEN_VAULT_KEY = "token"  # noqa: S105  # pragma: allowlist secret
WITAN_ADMIN_ACTOR_ID = "svc-witan-admin"
# The identity the code-graph pipeline uses, and the identity maintenance falls
# back to in environments where the admin principal is not provisioned yet.
WITAN_CI_ACTOR_ID = "svc-witan-ci"

##############################################
#   Vault auth binding (VSO sync only)        #
##############################################
# witan needs no AWS access (no IAM policy attached, iam_policy_document=None);
# the IRSA service account is required by the binding but unused — witan's pods
# are created by the ToolHive operator with its own service account. The
# binding exists for the Vault Secrets Operator sync wiring below. Same shape
# as toolhive_swe's own no-AWS binding.
witan_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="witan",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("witan_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="witan",
        vault_sync_service_account_names=["witan-vault"],
        k8s_labels=k8s_labels,
    )
)

witan_ci_token_secret = OLVaultK8SSecret(
    f"witan-ci-token-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=WITAN_CI_TOKEN_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=WITAN_CI_TOKEN_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="witan/ci-token",
        exclude_raw=True,
        excludes=[".*"],
        templates={
            WITAN_CI_TOKEN_SECRET_KEY: (
                f'{{{{ get .Secrets "{WITAN_CI_TOKEN_VAULT_KEY}" }}}}'
            )
        },
        refresh_after="1h",
        vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=witan_auth_binding.vault_k8s_resources,
    ),
)

# The MCP tier's credential for its own server-scoped questions (graph_list).
# Its own Secret rather than a second reference to witan-ci-token — see
# WITAN_CODE_TOKEN_SECRET_NAME — which is what lets the Vault path move here
# without touching the MCPServer spec.
#
# svc-witan where it is provisioned, svc-witan-ci where it is not. The fallback
# is not a supported operating mode so much as the only thing that yields a
# working Secret in an environment with no omnigraph SOPS file: reading an
# absent Vault path would sync an empty Secret, and the tier would send an empty
# bearer token and be denied every enumeration.
witan_code_token_secret = OLVaultK8SSecret(
    f"witan-code-token-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=WITAN_CODE_TOKEN_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=WITAN_CODE_TOKEN_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path=("witan/service-token" if service_token_provisioned else "witan/ci-token"),
        exclude_raw=True,
        excludes=[".*"],
        templates={
            WITAN_CODE_TOKEN_SECRET_KEY: (
                # Both paths store the raw token under the same "token" key, so
                # the template is the same either way.
                f'{{{{ get .Secrets "{WITAN_CI_TOKEN_VAULT_KEY}" }}}}'
            )
        },
        refresh_after="1h",
        vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=witan_auth_binding.vault_k8s_resources,
    ),
)

# svc-witan-admin's token, in environments whose omnigraph stack provisioned it.
# Consumed by the pre-deploy migration Job and the break-glass pod template
# below, and by nothing that serves traffic — the MCP tier must not hold a
# credential that can rewrite memory rows out from under a per-user actor.
witan_admin_token_secret = None
if admin_token_provisioned:
    witan_admin_token_secret = OLVaultK8SSecret(
        f"witan-admin-token-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=WITAN_ADMIN_TOKEN_SECRET_NAME,
            namespace=NAMESPACE,
            labels=k8s_global_labels,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=WITAN_ADMIN_TOKEN_SECRET_NAME,
            dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
            mount="secret-operations",
            mount_type="kv-v1",
            path="witan/admin-token",
            exclude_raw=True,
            excludes=[".*"],
            templates={
                WITAN_ADMIN_TOKEN_SECRET_KEY: (
                    f'{{{{ get .Secrets "{WITAN_ADMIN_TOKEN_VAULT_KEY}" }}}}'
                )
            },
            # Rotating this is a deliberate act (edit the SOPS file, redeploy the
            # omnigraph stack), and nothing consumes it continuously — the two
            # consumers are a per-deploy Job and a pod an operator starts by
            # hand, both of which read it fresh at pod start. Polling it as often
            # as the per-user map would be checking for a change that only ever
            # arrives with a deploy.
            refresh_after="1h",
            vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=witan_auth_binding.vault_k8s_resources,
        ),
    )

# Which principal in-cluster maintenance authenticates as. svc-witan-admin where
# it exists; svc-witan-ci otherwise, which is what every environment does today
# and is exactly what this replaces — the code-graph pipeline's identity, used on
# the memory graph agent-kit's Cedar bundle grants it no access to. The fallback
# is explicit and temporary rather than silent: it keeps a first deploy of this
# change from breaking the migration gate in an environment whose SOPS file has
# not been updated, and it disappears from every environment the moment those two
# keys are added. See docs/witan-admin-break-glass-runbook.md.
if witan_admin_token_secret is not None:
    maintenance_actor_id = WITAN_ADMIN_ACTOR_ID
    maintenance_token_secret_name = WITAN_ADMIN_TOKEN_SECRET_NAME
    maintenance_token_secret_key = WITAN_ADMIN_TOKEN_SECRET_KEY
    maintenance_token_secret: OLVaultK8SSecret = witan_admin_token_secret
else:
    maintenance_actor_id = WITAN_CI_ACTOR_ID
    maintenance_token_secret_name = WITAN_CI_TOKEN_SECRET_NAME
    maintenance_token_secret_key = WITAN_CI_TOKEN_SECRET_KEY
    maintenance_token_secret = witan_ci_token_secret

actor_tokens_secret = OLVaultK8SSecret(
    f"witan-actor-tokens-secret-{stack_info.env_suffix}",
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
        # Deliberately NO restart_targets here, unlike the omnigraph stack's
        # copy of this same secret. The asymmetry is real, not an omission:
        # witan's ActorTokenResolver (agent-kit
        # packages/witan-core/witan_core/identity.py) re-stats this file and
        # reloads it on any cache miss, so a newly-synced actor's token is
        # live on that actor's very next request with no restart. It is
        # omnigraph-server that hashes the map once at boot and never looks
        # again, which is why only that Deployment needs bouncing.
        vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=witan_auth_binding.vault_k8s_resources,
    ),
)

# The App's private key, synced into the namespace from the Vault path this
# stack wrote above. Same SOPS -> Vault -> VSO shape the omnigraph stack uses
# for the actor tokens, so the encrypted source is version-controlled and a
# rebuilt Vault can be repopulated by a `pulumi up` rather than by finding
# whoever still has the PEM — GitHub only lets it be downloaded once.
github_app_secret = None
if github_app_vault_secret is not None:
    github_app_secret = OLVaultK8SSecret(
        f"witan-github-app-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=GITHUB_APP_SECRET_NAME,
            namespace=NAMESPACE,
            labels=k8s_global_labels,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=GITHUB_APP_SECRET_NAME,
            dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
            mount="secret-operations",
            mount_type="kv-v1",
            path="witan/github-app",
            exclude_raw=True,
            excludes=[".*"],
            templates={
                GITHUB_APP_SECRET_KEY: (
                    f'{{{{ get .Secrets "{GITHUB_APP_VAULT_KEY}" }}}}'
                )
            },
            # The key itself never rotates on a schedule; this only needs to
            # pick up a deliberate re-issue, so it is checked far less often
            # than the per-user token map next door.
            refresh_after="24h",
            vaultauth=witan_auth_binding.vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[
                witan_auth_binding.vault_k8s_resources,
                github_app_vault_secret,
            ],
        ),
    )

#########################################
#   witan image (built + repo owned by   #
#   the pulumi-witan Concourse pipeline) #
#########################################
# The ``witan`` ECR repository is created (idempotently) by the Concourse
# build job on every push, not managed here -- see this module's docstring.
# One repo is shared across CI/QA/Production (same AWS account); the image
# is pinned by digest (WITAN_DOCKER_SHA, set by the build job) so a new push
# actually changes this stage's Deployment pod spec.
witan_aws_account = aws.get_caller_identity()
witan_image_repository = (
    f"{witan_aws_account.account_id}.dkr.ecr.{aws_config.region}.amazonaws.com/witan"
)
witan_image = format_docker_image_ref(witan_image_repository, "WITAN")

#########################################
#   Pre-deploy witan data migrations     #
#########################################
# witan's own backfills only — schema convergence belongs to the omnigraph
# stack's `cluster apply` step, which runs before its server restarts. Gated
# ahead of the MCPServer below via depends_on, so the new image never serves
# against a graph its migrations haven't run over. See migrations.py.
witan_migration_job = create_migration_job(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    witan_image=witan_image,
    omnigraph_server_addr=omnigraph_server_addr,
    council_graph_id=council_graph_id,
    maintenance_actor_id=maintenance_actor_id,
    maintenance_token_secret_name=maintenance_token_secret_name,
    maintenance_token_secret_key=maintenance_token_secret_key,
    maintenance_token_secret=maintenance_token_secret,
)

#########################################
#   Break-glass maintenance template     #
#########################################
# A suspended CronJob carrying the pod spec an operator instantiates by hand for
# the ADR-0005 path (b) operations the MCP path refuses (schema apply, storage
# rebuild, store merge, cross-actor debugging). Declared only where the admin
# principal exists: the alternative would be a break-glass pod that runs as the
# code-graph pipeline, which is the thing this task exists to stop.
witan_break_glass = None
if witan_admin_token_secret is not None:
    witan_break_glass = create_break_glass_cronjob(
        stack_info=stack_info,
        namespace=NAMESPACE,
        k8s_global_labels=k8s_global_labels,
        witan_image=witan_image,
        omnigraph_server_addr=omnigraph_server_addr,
        council_graph_id=council_graph_id,
        admin_actor_id=WITAN_ADMIN_ACTOR_ID,
        admin_token_secret_name=WITAN_ADMIN_TOKEN_SECRET_NAME,
        admin_token_secret_key=WITAN_ADMIN_TOKEN_SECRET_KEY,
        admin_token_secret=witan_admin_token_secret,
    )

#########################################
#   MCPGroup + witan MCPServer           #
#########################################
mcp_servers = create_mcp_servers(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    cluster_stack=cluster_stack,
    witan_image=witan_image,
    omnigraph_server_addr=omnigraph_server_addr,
    council_graph_id=council_graph_id,
    oidc_issuer=KEYCLOAK_ISSUER,
    oidc_audience=WITAN_OIDC_AUDIENCE,
    actor_tokens_secret_name=ACTOR_TOKENS_SECRET_NAME,
    actor_tokens_secret=actor_tokens_secret,
    witan_ci_token_secret_name=WITAN_CI_TOKEN_SECRET_NAME,
    witan_ci_token_secret_key=WITAN_CI_TOKEN_SECRET_KEY,
    witan_ci_token_secret=witan_ci_token_secret,
    witan_code_token_secret_name=WITAN_CODE_TOKEN_SECRET_NAME,
    witan_code_token_secret_key=WITAN_CODE_TOKEN_SECRET_KEY,
    witan_code_token_secret=witan_code_token_secret,
    migration_job=witan_migration_job,
)

#########################################
#   MCPOIDCConfig (incoming validation)  #
#########################################
# Points at Keycloak's REAL issuer (not a vMCP-local one) — see module
# docstring for why this is the "External OIDC provider" scenario, not
# toolhive_swe's "Embedded auth server" one.
mcp_oidc_config = kubernetes.apiextensions.CustomResource(
    f"witan-mcp-oidc-config-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPOIDCConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=MCP_OIDC_CONFIG_NAME,
        namespace=NAMESPACE,
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
    f"witan-vmcp-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="VirtualMCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="witan-vmcp",
        namespace=NAMESPACE,
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
            # Every Secret the backend MCPServer consumes, restated here even
            # though `*mcp_servers.servers` already carries them: Pulumi orders
            # transitively, so this changes nothing the engine does. It is kept
            # so the list reads as the complete set of things that must exist
            # before the aggregator does — a Secret missing from it looks like
            # an oversight rather than a deliberate omission.
            witan_ci_token_secret,
            witan_code_token_secret,
            actor_tokens_secret,
        ]
    ),
)

#########################################
#   Internet exposure via APISIX         #
#########################################
vmcp_cert, vmcp_httproute = create_ingress_resources(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    vmcp_domain=VMCP_DOMAIN,
    witan_virtualmcpserver=witan_virtualmcpserver,
)

#########################################
#   CI code-graph indexer (CronJob)      #
#########################################
# The single entitled writer of every managed repo's shared code graph. Not
# gated on the vMCP or the MCPServer: it writes the data tier directly and is
# useful — arguably most useful — while the serving tier is still rolling.
witan_ci_indexer = create_ci_indexer(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    witan_image=witan_image,
    omnigraph_server_addr=omnigraph_server_addr,
    managed_repos=managed_repos,
    schedule=WITAN_CI_INDEX_SCHEDULE,
    witan_ci_token_secret_name=WITAN_CI_TOKEN_SECRET_NAME,
    witan_ci_token_secret_key=WITAN_CI_TOKEN_SECRET_KEY,
    witan_ci_token_secret=witan_ci_token_secret,
    github_app_id=WITAN_GITHUB_APP_ID if github_app_secret else None,
    github_app_installation_id=(
        WITAN_GITHUB_APP_INSTALLATION_ID if github_app_secret else None
    ),
    github_app_secret_name=GITHUB_APP_SECRET_NAME if github_app_secret else None,
    github_app_secret=github_app_secret,
)

export("namespace", NAMESPACE)
export("mcp_group_name", MCP_GROUP_NAME)
export("ci_indexer_schedule", WITAN_CI_INDEX_SCHEDULE if witan_ci_indexer else None)
export("vmcp_domain", VMCP_DOMAIN)
export("vmcp_oidc_issuer", KEYCLOAK_ISSUER)
export("witan_image_repository", witan_image_repository)
export("omnigraph_server_addr", omnigraph_server_addr)
# Which principal ran this environment's migrations, and the name to pass to
# `kubectl create job --from=cronjob/…` for a break-glass pod (null where the
# admin principal is not provisioned yet, in which case there is no such
# CronJob). Exported so the runbook's first step — "check which identity this
# environment is on" — is a `pulumi stack output` rather than reading two files.
export("maintenance_actor_id", maintenance_actor_id)
export(
    "break_glass_cronjob",
    BREAK_GLASS_CRONJOB_NAME if witan_break_glass is not None else None,
)
