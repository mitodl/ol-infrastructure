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
    hence no embedded broker, no persistent signing keys, no Redis). See
    ADR-0009's Resolution addendum and agent-kit ADR-0004's matching
    Resolution addendum (2026-07-10) for the full decision record.

    This also means clients need an already-valid Keycloak JWT with the right
    audience before calling — there is no vMCP-brokered interactive login
    here. That is intentional (agent-kit ADR-0004 D3: per-user omnigraph
    bearer tokens are pre-provisioned out-of-band, not minted on the fly), but
    it does mean whatever normally gets a human or CI agent a Keycloak JWT for
    other internal tools (existing SSO session, device-code flow, etc.) is a
    prerequisite this stack does not itself provide.

Outgoing auth — why two settings are needed to forward one token:

    Having no embedded broker to substitute a token does NOT mean ToolHive
    forwards the client's. It forwards **nothing** unless told to. This stack
    originally assumed otherwise, and the result was a total, silent outage:
    the vMCP called the backend with no credential at all, witan's JWTVerifier
    returned 401, and — because a backend whose resolved strategy is
    ``unauthenticated`` has its 401 read as genuine misconfiguration rather
    than as proof of life — the vMCP marked its only backend
    ``unauthenticated`` and excluded it from capability aggregation. Clients
    got a successful ``initialize`` and then ``tools/list`` failed, so every
    external signal (pod ready, route resolving, TLS, 200 + session id) looked
    healthy while the endpoint served zero tools. Two settings fix it, and
    both are required:

    - ``passthroughHeaders: ["Authorization"]`` is what actually forwards the
      user's Keycloak JWT to witan. It applies to real session traffic only.
    - ``outgoingAuth.backends.witan`` points at a ``headerInjection``
      ``MCPExternalAuthConfig``. This is NOT how the backend authenticates —
      witan ignores the injected header entirely. It exists because the
      periodic backend health probe runs on a background context that carries
      no client request and therefore no passthrough header, so the probe will
      always 401. ToolHive treats a probe 401 as healthy when the backend has
      *some* non-``unauthenticated`` strategy configured, and as a
      misconfiguration when it does not. Configuring any such strategy is
      what keeps the backend in the aggregated view; header injection is the
      only one that neither sets ``Authorization`` (which would clobber the
      passthrough token) nor needs new Keycloak plumbing.

      That 401-is-healthy rule is upstream's deliberate, tested design, not an
      inference from observed behavior. At ToolHive v0.40.1 it is
      ``authErrorStatus`` (``pkg/vmcp/health/checker.go:192``), reached from
      ``categorizeError`` (same file, :154 and :168), and pinned by
      ``TestHealthChecker_CheckHealth_AuthErrorWithOutgoingAuthIsHealthy``
      (``pkg/vmcp/health/checker_test.go:691``) — whose table includes a
      ``header_injection`` row asserting ``BackendHealthy`` and a nil error.
      Cited because the claim is not verifiable from this repository, and the
      whole outage came from an unverified assumption about this same hop.

    The alternative, ``tokenExchange``, would authenticate the probe properly
    via client-credentials instead of relying on 401-as-healthy, and would
    still preserve per-user identity on real traffic. It is not used here
    because it requires a confidential Keycloak client, realm-level token
    exchange, and a rotated secret — where passthrough needs none of that,
    since ``witan-cli`` already mints tokens with the ``witan`` audience that
    witan's own verifier checks. Revisit if the probe's 401 noise or the
    forwarded-header trust model becomes a problem.

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
    WITAN_MCPSERVER_NAME,
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
from ol_infrastructure.lib.k8s_vpa import make_vpa
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    format_docker_image_ref,
    get_docker_image_tag,
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

# Client-side write admission, applied inside the MCP tier before a write is
# sent to the data tier (agent-kit `witan_core.omnigraph._WriteGate`). This is
# the GLOBAL bound the data tier's per-actor cap cannot be: every user's write
# passes through this single-replica pod, so it is the one place total in-flight
# concurrency is visible. Past ~4 writes in flight against one graph, a write
# cannot finish inside ToolHive's hardcoded 30s deadline, and what the caller
# gets is not a slow write but a 502 whose outcome is indeterminate — so the
# tier refuses with a sentence instead, before anything is sent.
#
# Empty here means "use the code default" (4 writes, 10s queue wait). Set per
# stack when an environment's measured knee differs — Production's larger graphs
# make each write slower, and the write cost itself is expected to change
# upstream. Both are read per call inside witan, so `kubectl set env` moves them
# on a live pod ahead of committing the config.
WITAN_REMOTE_WRITE_MAX_INFLIGHT = witan_config.get("remote_write_max_inflight") or ""
WITAN_REMOTE_WRITE_QUEUE_SECONDS = witan_config.get("remote_write_queue_seconds") or ""

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
# `service.version` on every span and metric this stack's workloads emit. The
# same tag-or-digest `witan_image` is built from, so a trace in Tempo names the
# exact artifact it came from and not a build-time guess at one.
witan_service_version = get_docker_image_tag("WITAN")

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
    service_version=witan_service_version,
    remote_write_max_inflight=WITAN_REMOTE_WRITE_MAX_INFLIGHT,
    remote_write_queue_seconds=WITAN_REMOTE_WRITE_QUEUE_SECONDS,
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
#   vMCP -> backend probe credential     #
#########################################
# Exists only to give the witan backend a non-`unauthenticated` outgoing-auth
# strategy. See the "Outgoing auth" section of this module's docstring for why
# that is load-bearing. The value is NOT a credential: witan never reads this
# header, and the vMCP -> backend hop is ClusterIP-only. It is a fixed marker
# because ToolHive's `headerInjection` strategy requires a non-empty
# `valueSecretRef`, so a Secret is the only shape the CRD accepts. Deliberately
# a plain Secret rather than a Vault-backed one — routing a constant with no
# secret value through Vault would imply a rotation story that does not exist.
VMCP_PROBE_HEADER_NAME = "X-Witan-Vmcp"
VMCP_PROBE_SECRET_NAME = "witan-vmcp-backend-probe"  # noqa: S105  # pragma: allowlist secret
VMCP_PROBE_SECRET_KEY = "value"  # noqa: S105  # pragma: allowlist secret
VMCP_BACKEND_AUTH_CONFIG_NAME = "witan-vmcp-backend-auth"

vmcp_probe_secret = kubernetes.core.v1.Secret(
    f"witan-vmcp-backend-probe-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=VMCP_PROBE_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
    ),
    string_data={VMCP_PROBE_SECRET_KEY: "witan-vmcp"},  # pragma: allowlist secret
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

vmcp_backend_auth_config = kubernetes.apiextensions.CustomResource(
    f"witan-vmcp-backend-auth-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPExternalAuthConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=VMCP_BACKEND_AUTH_CONFIG_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "type": "headerInjection",
        "headerInjection": {
            "headerName": VMCP_PROBE_HEADER_NAME,
            "valueSecretRef": {
                "name": VMCP_PROBE_SECRET_NAME,
                "key": VMCP_PROBE_SECRET_KEY,
            },
        },
    },
    opts=ResourceOptions(depends_on=[vmcp_probe_secret]),
)

#########################################
#   VirtualMCPServer aggregator          #
#########################################
# No authServerConfig block: unlike toolhive_swe, this vMCP is NOT an OAuth
# provider of its own. incomingAuth validates the client's genuine Keycloak
# JWT directly, and `passthroughHeaders` then forwards that same JWT to the
# witan MCPServer unmodified — which is exactly the "External OIDC provider"
# scenario ADR-0009's Resolution addendum specifies. The forwarding is
# explicit: ToolHive does not do it by default. See the "Outgoing auth"
# section of this module's docstring.
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
        # The client's Keycloak bearer token, forwarded verbatim to the backend
        # so witan's own JWTVerifier sees the *user's* token and derives the
        # per-request actor from its `sub` (ADR-0004 D1/D2). Header-forwarding
        # runs outermost on the outbound chain and is skip-if-present, so the
        # header-injection strategy below (a different header name) coexists
        # with it rather than overwriting it.
        "passthroughHeaders": ["Authorization"],
        "outgoingAuth": {
            # `discovered` inspects each backend's own externalAuthConfigRef;
            # witan's MCPServer deliberately has none (see mcp_servers.py), so
            # the per-backend override below is what actually applies.
            "source": "discovered",
            "backends": {
                WITAN_MCPSERVER_NAME: {
                    "type": "externalAuthConfigRef",
                    "externalAuthConfigRef": {"name": VMCP_BACKEND_AUTH_CONFIG_NAME},
                },
            },
        },
        "serviceType": "ClusterIP",
        # VirtualMCPServerSpec has no `resources` field of its own (unlike
        # MCPServer), so the aggregator's limits can only be set through the
        # documented PodTemplateSpec escape hatch, targeting the operator-managed
        # `vmcp` container by name — the same mechanism mcp_servers.py uses for
        # the backend.
        #
        # ── WHY THIS EXISTS ──
        # The operator's default is 500m CPU / 512Mi memory, and 512Mi is not
        # enough. Measured against CI on 2026-08-07: the vMCP holds per-session
        # aggregated capability state — its log emits `session capabilities
        # injected from core … tool_count:67` once PER SESSION — and grows from
        # 17Mi idle to past 512Mi at ~32 concurrent sessions, i.e. roughly 15Mi
        # resident per session. It was OOMKilled (`exitCode: 137`), APISIX lost
        # its only upstream, and every client got a 502 HTML page until it came
        # back. Two identical 32-client bursts four seconds apart read
        # `{200: 32}` then `{200: 3, 502: 29}` — the first burst killed it.
        #
        # Nothing else in the path was under any strain: the proxy runner stayed
        # Ready at 11m against its 500m CPU limit (~2%) and the backend never
        # restarted. This is a memory ceiling and nothing else, which is also why
        # replicas are not the answer to it — see the VPA note below and the same
        # lesson already learned in `infrastructure/aws/eks/traefik.py`.
        #
        # ── SIZING ──
        # 2Gi supports roughly 130 concurrent SESSIONS at the measured
        # ~15Mi/session. Sessions, not people: the burst above opened all 32
        # from a single token, so one user running a fleet of agents can hold
        # many at once and the count of provisioned realm users says nothing
        # about the ceiling. Size against expected concurrent sessions.
        #
        # ★ The 4:1 limit:request ratio is load-bearing, not incidental. The VPA
        # below controls this container with `controlledValues:
        # RequestsAndLimits`, which scales the limit to PRESERVE this ratio while
        # bounding only the request. So the ratio chosen here, multiplied by the
        # VPA's `maxAllowed`, is what actually caps memory: 4 x 1Gi = 4Gi. Widen
        # the ratio (say a 256Mi request against the same 2Gi limit) and the
        # effective ceiling silently becomes 8Gi, which would let a leak grow
        # until it evicts its node-mates rather than failing loudly.
        #
        # The per-session figure is inferred from a single OOM boundary, not a
        # sweep — treat it as a floor to revise once the VPA has real numbers.
        # Reproduce with agent-kit's `python -m witan.scripts.concurrency_probe`.
        "podTemplateSpec": {
            "spec": {
                "containers": [
                    {
                        "name": "vmcp",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "512Mi"},
                            "limits": {"cpu": "500m", "memory": "2Gi"},
                        },
                    }
                ],
            }
        },
        "config": {
            # `priority`, NOT the CRD's `prefix` default, because prefix mode
            # renames unconditionally: it does no conflict detection at all, so
            # with witan as the group's only member it still rewrote all 65
            # tools to `witan_*` and no client asking for `memory_search` could
            # find them. `priority` leaves a tool whose name is unique across
            # the group exactly as the backend published it, which — since
            # nothing else is in the group — is every tool.
            #
            # There is no "don't rename anything" setting: the enum is
            # prefix/priority/manual and an empty prefixFormat is rejected, so
            # bare names have to come from one of the other two strategies.
            # `manual` would also work; `priority` is chosen because its one
            # downside (on a name collision the loser is dropped with only a
            # log line, where manual fails the whole tools/list loudly) needs
            # two backends to be reachable, and witan-code is mounted
            # in-process rather than deployed separately.
            "aggregation": {
                "conflictResolution": "priority",
                # Required and must be non-empty — the vMCP refuses to start
                # otherwise, and the CRD does not catch it, so a missing entry
                # here is a CrashLoop rather than a rejected apply.
                "conflictResolutionConfig": {"priorityOrder": [WITAN_MCPSERVER_NAME]},
            },
        },
    },
    opts=ResourceOptions(
        depends_on=[
            mcp_servers.group,
            *mcp_servers.servers,
            mcp_oidc_config,
            vmcp_backend_auth_config,
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
#   Vertical rightsizing (VPA)           #
#########################################
# Memory only, deliberately. Memory is the resource with a demonstrated failure:
# the aggregator died of it, and the backend's limits were never applied at all.
# No CPU pressure has been observed on either of these two workloads — but note
# that is an absence of evidence, not a measurement: the only CPU figure taken
# during the incident was the PROXY RUNNER's (11m against its 500m limit), and
# that is a third workload, not one of the two targeted here. Leaving CPU
# uncontrolled therefore rests on memory being the known problem, not on proven
# CPU headroom; it also keeps the door open for a CPU-based HPA later without
# re-creating the known HPA/VPA conflict that
# `infrastructure/aws/eks/traefik.py` and `apisix_official.py` both document.
#
# This is the same remedy, for the same failure, as the one traefik.py describes:
# a per-pod memory ceiling with no headroom, where "adding replicas doesn't help
# ... it just means more pods hitting the same wall". Worth stating plainly here
# because the instinct on seeing 502s under load is to reach for replicas, and
# for this failure that would have changed nothing.
#
# `minAllowed` is what stops the fix from undoing itself: VPA sizes from observed
# usage, and witan sits idle at ~17Mi for long stretches, so an unbounded
# recommender would shrink the aggregator back toward its idle footprint and
# re-introduce the OOM on the next burst. The floors below are the measured safe
# sizes, not recommendations to be optimised away.
#
# ★ THESE TARGETS ARE SINGLETONS, AND THAT NORMALLY DEFEATS THE VPA UPDATER.
# The updater refuses to touch a controller with fewer than `--min-replicas`
# pods (upstream default 2, and not overridden on this cluster), which would
# leave both VPAs below computing recommendations that never reach a pod. What
# rescues it is `--in-place-skip-disruption-budget=true` on the
# `vertical-pod-autoscaler-updater` Deployment in `kube-system`, combined with
# `make_vpa`'s unconditional `InPlaceOrRecreate` mode — vpa-updater 1.7.1,
# `pkg/updater/restriction/pods_restriction_factory.go`:
#
#     skipReplicaCheck := (usingInPlaceOrRecreate || usingInPlace) &&
#                         f.inPlaceSkipDisruptionBudget
#
# and, for a controller whose live pod count is under `required`: when
# `skipReplicaCheck` is false it is skipped outright (`continue`), and when true
# it is admitted with `isBelowMinReplicas = true` instead.
#
# So a 1-replica controller IS admitted, flagged `belowMinReplicas`. That flag
# then makes `CanEvict` return false while `CanInPlaceUpdate` still approves —
# which is exactly the behaviour wanted here: resize the live pod, never evict
# it. Evicting either of these singletons would drop APISIX's only upstream and
# recreate the very 502 this stack is fixing.
#
# The dependency is invisible from here, so: if that updater flag is ever
# removed, or `min-replicas` is set per-VPA, these two VPAs silently degrade to
# recommendation-only and the OOM protection above goes with them. Raised by
# review on PR #5320 as a suspected defect; verified against the running
# updater's args and the 1.7.1 source rather than assumed either way.
#
# Only the two workloads whose containers this stack actually declares. The
# ToolHive proxy runner Deployment is left alone: it showed ~20Mi against a
# 512Mi limit, its RESOURCES are not settable through the MCPServer CRD, and
# adding a VPA to a workload under no pressure is churn for its own sake.
#
# Resources specifically, not the whole resource. `resourceOverrides
# .proxyDeployment` carries annotations, labels, podTemplateMetadataOverrides,
# `env` and imagePullSecrets — there is simply no resources/limits field among
# them, and no podTemplateSpec either (the one on MCPServer patches the MCP
# workload's StatefulSet, not this Deployment). The `env` half is load-bearing
# elsewhere: WITAN_PROXY_HEALTH_ENV in mcp_servers.py rides it to stop a write
# burst getting this same container killed by its own liveness probe.
make_vpa(
    f"witan-vmcp-vpa-{stack_info.env_suffix}",
    namespace=NAMESPACE,
    target_kind="Deployment",
    target_name="witan-vmcp",
    container_name="vmcp",
    controlled_resources=["memory"],
    # Floor = the request declared on the vMCP above; ceiling x the 4:1 ratio
    # there = a 4Gi hard cap on the limit. See that comment.
    min_allowed={"memory": "512Mi"},
    max_allowed={"memory": "1Gi"},
    disable_other_containers=True,
    opts=ResourceOptions(depends_on=[witan_virtualmcpserver]),
)

make_vpa(
    f"witan-backend-vpa-{stack_info.env_suffix}",
    namespace=NAMESPACE,
    # The backend is a StatefulSet named `witan`; the proxy runner is a
    # Deployment ALSO named `witan` in this same namespace. Only `target_kind`
    # separates them, so this is not a place to guess.
    target_kind="StatefulSet",
    target_name=WITAN_MCPSERVER_NAME,
    container_name="mcp",
    controlled_resources=["memory"],
    # Floor = WITAN_BACKEND_RESOURCES' request. 2:1 ratio there, so the 1Gi
    # ceiling caps the limit at 2Gi.
    min_allowed={"memory": "256Mi"},
    max_allowed={"memory": "1Gi"},
    disable_other_containers=True,
    opts=ResourceOptions(depends_on=[*mcp_servers.servers]),
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
    service_version=witan_service_version,
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
