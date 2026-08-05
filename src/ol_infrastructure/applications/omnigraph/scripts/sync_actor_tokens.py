"""Reconcile the omnigraph actor-token map against Keycloak group membership.

agent-kit ADR-0004 D3 gives every witan user their own omnigraph bearer token,
keyed by the actor id derived from their Keycloak ``sub``. This script is the
process that mints and retires those tokens. It is the *sole writer* of
``secret-operations/witan/actor-tokens``, the one Vault artifact both
omnigraph-server (``OMNIGRAPH_SERVER_BEARER_TOKENS_FILE``) and the witan MCP
tier (``WITAN_ACTOR_TOKENS_FILE``) resolve tokens from.

WHAT IT COMPUTES

    actor-tokens  =  service-tokens  +  one act-<sub> entry per witan-users member

``service-tokens`` is the Pulumi/SOPS-owned map of non-human actors
(``svc-witan-ci``, and later ``svc-witan-admin``). Splitting it out of
``actor-tokens`` is what lets this script own the merged output outright: two
writers on one Vault path means every ``pulumi up`` silently reverts every
per-user entry, and every user 401s until the next run of this job. So Pulumi
owns the *input*, this job owns the *derived output*, and each path has exactly
one writer. See ``token_sync.py`` for the deployment side of that split.

The output is a pure function of its two inputs, which is the property worth
protecting: a token already provisioned for a still-present member is carried
over verbatim rather than re-minted, so a steady-state run is a no-op and
writes nothing. That matters more than it looks — the actor-tokens Vault secret
carries a VSO ``rolloutRestartTarget``, so *every* write to it bounces
omnigraph-server, and the data tier is replicas=1/Recreate (a hard ~10-30s
graph outage, absorbed by client-side connect retry). Writing only on a real
change is what keeps that cost proportional to actual membership churn.

Users are never shown these tokens and never present them. A user
authenticates to the witan tier with an OIDC JWT; witan maps their ``sub`` to
``act-<sub>``, looks the token up in this map, and presents it to
omnigraph-server on their behalf. The token is an internal capability, so it is
opaque random bytes with no format requirements and nothing to distribute.

DELIBERATELY STDLIB-ONLY

Both the Keycloak Admin API and the Vault HTTP API are plain JSON over HTTPS,
so this needs no third-party client and therefore no image to build, publish,
or keep patched — it runs on a stock ``python:3.12-slim``. Keep it that way;
an ``import`` of anything outside the standard library turns a ConfigMap into a
release pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger("sync-actor-tokens")

# Mirrors witan_core.identity.derive_actor_id (agent-kit
# packages/witan-core/witan_core/identity.py). These two derivations MUST agree:
# witan resolves a token by the id it derives from a validated JWT, and this
# script files the token under the id it derives here. A divergence has no
# symptom until a user whose sub happens to contain the differing characters
# gets a 401 that looks like a provisioning lag.
ACTOR_PREFIX = "act-"
_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")

# Length of a freshly minted token, in bytes of entropy before base64url
# encoding. These are bearer tokens for a service with no rate limiting in
# front of it, so they are sized to be brute-force-irrelevant rather than
# typeable — nobody ever reads one.
TOKEN_ENTROPY_BYTES = 32

# The key the {actor_id: token} map is stored under inside each Vault secret,
# as a JSON *string* (so the secret has one scalar field, not one field per
# actor). Must match ACTOR_TOKENS_VAULT_KEY in the omnigraph Pulumi stack and
# the VSO template that renders it into the mounted file.
TOKENS_VAULT_KEY = "tokens_json"  # pragma: allowlist secret

# Keycloak caps a members page well below this in some versions; it returns
# fewer than asked rather than erroring, and the loop below keys off that.
MEMBERS_PAGE_SIZE = 100

# Guard against a paging bug turning into an unbounded loop against Keycloak.
# 200 pages at the size above is 20k members — orders of magnitude above any
# plausible witan-users group.
MAX_MEMBER_PAGES = 200

HTTP_TIMEOUT_SECONDS = 30


class SyncError(Exception):
    """A condition that must stop the run before anything is written."""


def derive_actor_id(sub: str) -> str:
    """Map a Keycloak ``sub`` (== the user's uuid) to an omnigraph actor id."""
    slug = _SANITIZE_RE.sub("-", sub.strip().lower()).strip("-")
    if not slug:
        msg = f"Cannot derive an actor id from sub={sub!r}"
        raise SyncError(msg)
    return f"{ACTOR_PREFIX}{slug}"


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    allow_404: bool = False,
) -> Any:
    """Issue one JSON HTTP request, returning the decoded body.

    ``allow_404`` returns ``None`` for a missing resource instead of raising —
    the one expected not-found is the actor-tokens path on a brand-new
    environment, where this job's first run is what creates it.
    """
    request = urllib.request.Request(  # noqa: S310 - https URLs from config
        url, method=method, data=data, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - https URLs from config
            request, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and allow_404:  # noqa: PLR2004
            return None
        detail = exc.read().decode("utf-8", "replace")[:500]
        msg = f"{method} {url} failed: HTTP {exc.code} {detail}"
        raise SyncError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"{method} {url} failed: {exc.reason}"
        raise SyncError(msg) from exc
    if not body:
        return None
    return json.loads(body)


###############################################################################
#   Vault                                                                      #
###############################################################################


def vault_login(vault_addr: str, auth_mount: str, role: str, jwt: str) -> str:
    """Exchange this pod's ServiceAccount JWT for a Vault token."""
    response = _request(
        f"{vault_addr}/v1/auth/{auth_mount}/login",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"role": role, "jwt": jwt}).encode(),
    )
    token = (response or {}).get("auth", {}).get("client_token")
    if not token:
        msg = f"Vault login at auth/{auth_mount} returned no client_token"
        raise SyncError(msg)
    return token


def read_token_map(vault_addr: str, vault_token: str, path: str) -> dict[str, str]:
    """Read a ``{actor_id: token}`` map out of a kv-v1 Vault secret.

    A missing secret is an empty map — the bootstrap case. A secret that exists
    but whose payload is unparseable is not: that is corruption, and continuing
    would rewrite the path with whatever we could still make sense of.
    """
    response = _request(
        f"{vault_addr}/v1/{path}",
        headers={"X-Vault-Token": vault_token},
        allow_404=True,
    )
    if response is None:
        LOG.info("vault path %s does not exist yet; treating as empty", path)
        return {}
    raw = (response.get("data") or {}).get(TOKENS_VAULT_KEY)
    if raw in (None, ""):
        LOG.info(
            "vault path %s has no %s key; treating as empty", path, TOKENS_VAULT_KEY
        )
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Vault path {path} key {TOKENS_VAULT_KEY} is not valid JSON: {exc}"
        raise SyncError(msg) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        msg = f"Vault path {path} key {TOKENS_VAULT_KEY} is not a {{str: str}} object"
        raise SyncError(msg)
    return parsed


def write_token_map(
    vault_addr: str, vault_token: str, path: str, tokens: dict[str, str]
) -> None:
    """Replace the kv-v1 secret at ``path`` with ``tokens``.

    kv-v1 writes replace the whole secret rather than patching it, which is
    exactly the semantics wanted here: the map is a computed artifact, so the
    write is a declaration of the complete desired state.
    """
    _request(
        f"{vault_addr}/v1/{path}",
        method="POST",
        headers={"X-Vault-Token": vault_token, "Content-Type": "application/json"},
        data=json.dumps(
            {TOKENS_VAULT_KEY: json.dumps(tokens, sort_keys=True)}
        ).encode(),
    )


###############################################################################
#   Keycloak                                                                   #
###############################################################################


def keycloak_token(
    base_url: str, realm: str, client_id: str, client_secret: str
) -> str:
    """Client-credentials grant for the token-sync service account."""
    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    response = _request(
        f"{base_url}/realms/{realm}/protocol/openid-connect/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form,
    )
    token = (response or {}).get("access_token")
    if not token:
        msg = f"Keycloak client-credentials grant for {client_id} returned no token"
        raise SyncError(msg)
    return token


def find_group_id(base_url: str, realm: str, token: str, group_name: str) -> str:
    """Resolve a top-level group name to its uuid.

    A missing group is fatal rather than "zero members". The two are
    indistinguishable in the member list but not in consequence: an empty group
    legitimately retires every per-user token, whereas a typo'd or not-yet-created
    group would do the same thing and call it success.
    """
    query = urllib.parse.urlencode({"search": group_name, "exact": "true"})
    groups = (
        _request(
            f"{base_url}/admin/realms/{realm}/groups?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        or []
    )
    for group in groups:
        if group.get("name") == group_name:
            return group["id"]
    msg = (
        f"Keycloak group {group_name!r} does not exist in realm {realm}. "
        "It is provisioned by the keycloak substructure stack "
        "(substructure/keycloak/ol_platform_engineering.py); this job will not "
        "retire every per-user token on the strength of a lookup that failed."
    )
    raise SyncError(msg)


def group_members(
    base_url: str, realm: str, token: str, group_id: str
) -> list[dict[str, Any]]:
    """Page through the group's direct members."""
    members: list[dict[str, Any]] = []
    for page in range(MAX_MEMBER_PAGES):
        query = urllib.parse.urlencode(
            {"first": page * MEMBERS_PAGE_SIZE, "max": MEMBERS_PAGE_SIZE}
        )
        batch = (
            _request(
                f"{base_url}/admin/realms/{realm}/groups/{group_id}/members?{query}",
                headers={"Authorization": f"Bearer {token}"},
            )
            or []
        )
        members.extend(batch)
        if len(batch) < MEMBERS_PAGE_SIZE:
            return members
    msg = (
        f"Group {group_id} still paging after {MAX_MEMBER_PAGES} pages; "
        "refusing to continue"
    )
    raise SyncError(msg)


###############################################################################
#   Reconciliation                                                             #
###############################################################################


def reconcile(
    service_tokens: dict[str, str],
    current: dict[str, str],
    members: list[dict[str, Any]],
) -> dict[str, str]:
    """Compute the desired actor-token map.

    Disabled Keycloak users are treated as non-members: leaving a token live
    for a disabled account would make "disable in Keycloak" a no-op for graph
    access, which is the one thing an operator disabling an account expects it
    not to be.
    """
    desired = dict(service_tokens)
    for member in members:
        sub = member.get("id")
        if not sub:
            msg = f"Keycloak returned a group member with no id: {member!r}"
            raise SyncError(msg)
        if not member.get("enabled", True):
            LOG.info("skipping disabled user %s", member.get("username", sub))
            continue
        actor_id = derive_actor_id(sub)
        if actor_id in service_tokens:
            # Can only happen if a service actor were named `act-<uuid>`, but
            # the collision would hand a human the service identity's token, so
            # it stops the run rather than resolving in either direction.
            msg = (
                f"Keycloak user {member.get('username', sub)} derives actor id "
                f"{actor_id}, which collides with a service actor. Refusing to "
                "overwrite either."
            )
            raise SyncError(msg)
        # Carried over verbatim when it already exists: re-minting a token for
        # an unchanged member would churn the map and bounce omnigraph-server
        # for no reason. Rotation is deliberately manual — delete the entry (or
        # the whole path) and let the next run re-mint.
        desired[actor_id] = current.get(actor_id) or secrets.token_urlsafe(
            TOKEN_ENTROPY_BYTES
        )
    return desired


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        msg = f"Required environment variable {name} is unset or empty"
        raise SyncError(msg)
    return value


def main() -> int:
    """Run one reconciliation pass, returning a process exit status."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    dry_run = os.environ.get("WITAN_TOKEN_SYNC_DRY_RUN", "").lower() in {
        "1",
        "true",
        "yes",
    }

    vault_addr = _require_env("VAULT_ADDR").rstrip("/")
    vault_auth_mount = _require_env("VAULT_K8S_AUTH_MOUNT").strip("/")
    vault_role = _require_env("VAULT_K8S_ROLE")
    sa_token_path = os.environ.get(
        "VAULT_K8S_SA_TOKEN_PATH",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    actor_tokens_path = _require_env("ACTOR_TOKENS_VAULT_PATH").strip("/")
    service_tokens_path = _require_env("SERVICE_TOKENS_VAULT_PATH").strip("/")

    keycloak_url = _require_env("KEYCLOAK_URL").rstrip("/")
    keycloak_realm = _require_env("KEYCLOAK_REALM")
    keycloak_client_id = _require_env("KEYCLOAK_CLIENT_ID")
    keycloak_client_secret = _require_env("KEYCLOAK_CLIENT_SECRET")
    group_name = _require_env("WITAN_USERS_GROUP")

    try:
        with open(sa_token_path, encoding="utf-8") as handle:  # noqa: PTH123
            sa_jwt = handle.read().strip()
    except OSError as exc:
        msg = f"Cannot read the ServiceAccount token at {sa_token_path}: {exc}"
        raise SyncError(msg) from exc

    vault_token = vault_login(vault_addr, vault_auth_mount, vault_role, sa_jwt)
    LOG.info("authenticated to vault at %s as role %s", vault_addr, vault_role)

    service_tokens = read_token_map(vault_addr, vault_token, service_tokens_path)
    # The whole output map is replaced on write, so an empty or unreadable
    # service map would silently retire svc-witan-ci — breaking the CI indexer
    # and the witan tier's own fallback client — and report success. The stack
    # that writes this path enforces the same invariant at `pulumi up` time;
    # this is the runtime half of it.
    if not service_tokens:
        msg = (
            f"Service token map at {service_tokens_path} is empty or missing. "
            "It is written by the omnigraph Pulumi stack from "
            f"src/bridge/secrets/omnigraph/secrets.<env>.yaml. Refusing to write "
            "an actor-token map that would drop every service identity."
        )
        raise SyncError(msg)
    if "svc-witan-ci" not in service_tokens:
        msg = (
            f"Service token map at {service_tokens_path} has no 'svc-witan-ci' "
            "entry. That identity backs the CI code-graph indexer and witan's "
            "own fallback client; a map without it is not one to publish."
        )
        raise SyncError(msg)

    current = read_token_map(vault_addr, vault_token, actor_tokens_path)

    access_token = keycloak_token(
        keycloak_url, keycloak_realm, keycloak_client_id, keycloak_client_secret
    )
    group_id = find_group_id(keycloak_url, keycloak_realm, access_token, group_name)
    members = group_members(keycloak_url, keycloak_realm, access_token, group_id)
    LOG.info("group %s (%s) has %d member(s)", group_name, group_id, len(members))

    desired = reconcile(service_tokens, current, members)

    added = sorted(set(desired) - set(current))
    removed = sorted(set(current) - set(desired))
    rotated = sorted(k for k in set(desired) & set(current) if desired[k] != current[k])
    LOG.info(
        "actors: %d current -> %d desired (+%d, -%d, ~%d)",
        len(current),
        len(desired),
        len(added),
        len(removed),
        len(rotated),
    )
    # Actor ids are uuid-derived and the tokens themselves never appear — these
    # names are what makes a run auditable after the fact.
    for actor_id in added:
        LOG.info("provisioning %s", actor_id)
    for actor_id in removed:
        LOG.info("retiring %s", actor_id)
    for actor_id in rotated:
        LOG.info("rotating %s", actor_id)

    if desired == current:
        LOG.info("actor-token map already matches Keycloak; nothing to write")
        return 0
    if dry_run:
        LOG.info("dry run: not writing %s", actor_tokens_path)
        return 0

    write_token_map(vault_addr, vault_token, actor_tokens_path, desired)
    # Worth saying explicitly: this write is what triggers the VSO
    # rolloutRestartTarget on the actor-tokens secret, and therefore a brief
    # omnigraph-server outage. If this line appears every run, something is
    # re-minting tokens that should have been carried over.
    LOG.info(
        "wrote %d actors to %s; omnigraph-server will be restarted by the "
        "Vault Secrets Operator",
        len(desired),
        actor_tokens_path,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as error:
        LOG.error("%s", error)  # noqa: TRY400 - the traceback adds nothing here
        sys.exit(1)
