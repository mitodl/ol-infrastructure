"""Reconcile the omnigraph actor-token map against Keycloak realm membership.

agent-kit ADR-0004 D3 gives every witan user their own omnigraph bearer token,
keyed by the actor id derived from their Keycloak ``sub``. This script is the
process that mints and retires those tokens. It is the *sole writer* of
``secret-operations/witan/actor-tokens``, the one Vault artifact both
omnigraph-server (``OMNIGRAPH_SERVER_BEARER_TOKENS_FILE``) and the witan MCP
tier (``WITAN_ACTOR_TOKENS_FILE``) resolve tokens from.

WHAT IT COMPUTES

    actor-tokens  =  service-tokens  +  one act-<sub> entry per human realm user

``service-tokens`` is the Pulumi/SOPS-owned map of non-human actors
(``svc-witan-ci``, and ``svc-witan-admin`` where provisioned). Splitting it out of
``actor-tokens`` is what lets this script own the merged output outright: two
writers on one Vault path means every ``pulumi up`` silently reverts every
per-user entry, and every user 401s until the next run of this job. So Pulumi
owns the *input*, this job owns the *derived output*, and each path has exactly
one writer. See ``token_sync.py`` for the deployment side of that split.

WHO COUNTS AS A USER

Every enabled, non-service-account user of the realm. The realm IS the
audience: ``ol-platform-engineering`` has ``registration_allowed=False``, no
identity-provider brokering and no federation, so its membership is already
exactly the people who should have witan. See :func:`realm_users` for why a
dedicated ``witan-users`` Keycloak group was removed rather than added, and
:func:`is_service_account` for the one class of realm user that is skipped.

The output is a pure function of its two inputs, which is the property worth
protecting: a token already provisioned for a still-present user is carried
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

# Keycloak caps a user page well below this in some versions; it returns fewer
# than asked rather than erroring, and the paging loop keys off that.
MEMBERS_PAGE_SIZE = 100

# Guard against a paging bug turning into an unbounded loop against Keycloak.
# 200 pages at the size above is 20k users — orders of magnitude above any
# plausible size for a hand-managed staff realm.
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
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # A 2xx whose body is not JSON means we are talking to something other
        # than the API we think we are — an HTML error page from a proxy, or a
        # misrouted URL. Left uncaught this is the one failure in the script
        # that escapes SyncError and reaches the operator as a bare traceback,
        # which names json/decoder.py rather than the request that was wrong.
        msg = (
            f"{method} {url} returned HTTP 200 with a non-JSON body "
            f"({body[:200]!r}): {exc}"
        )
        raise SyncError(msg) from exc


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

    An ABSENT secret is an empty map — the bootstrap case, where this job's
    first run is what creates the path. A secret that EXISTS but is not what we
    write is not: that is corruption, and continuing would rewrite the path
    with whatever we could still make sense of.

    The distinction matters most for actor-tokens, whose only writer is this
    job. Treating a present-but-malformed payload as empty would carry no
    existing token over, re-mint one for every user, and — because that write
    trips the VSO restart target — bounce omnigraph-server, invalidating every
    live session to recover from what may be a transient read problem.
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
        msg = (
            f"Vault path {path} exists but has no non-empty {TOKENS_VAULT_KEY} "
            "key. Every writer of this path sets it, so this is a malformed or "
            "partially-written secret rather than an empty one — refusing to "
            "treat it as no tokens and re-mint the whole map."
        )
        raise SyncError(msg)
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


def realm_users(base_url: str, realm: str, token: str) -> list[dict[str, Any]]:
    """Page through every user in the realm.

    The realm IS the audience. ``ol-platform-engineering`` has
    ``registration_allowed=False``, no identity-provider brokering and no
    federation — membership is hand-managed and already limited to the people
    who should have witan. A dedicated ``witan-users`` group inside it would be
    a second gate on an already-gated population, and its failure mode is the
    bad one: somebody is added to the realm, nobody adds them to the group, and
    they get a 401 that reads like a provisioning lag.

    (``witan-users`` still exists as a *Cedar* group in agent-kit's policy
    bundles, populated with the ``act-<sub>`` ids this job writes. Only its
    source changed — from a Keycloak group of the same name to the realm's
    human users. The name collision is what made the group look mandatory.)
    """
    users: list[dict[str, Any]] = []
    for page in range(MAX_MEMBER_PAGES):
        query = urllib.parse.urlencode(
            {"first": page * MEMBERS_PAGE_SIZE, "max": MEMBERS_PAGE_SIZE}
        )
        batch = (
            _request(
                f"{base_url}/admin/realms/{realm}/users?{query}",
                headers={"Authorization": f"Bearer {token}"},
            )
            or []
        )
        users.extend(batch)
        if len(batch) < MEMBERS_PAGE_SIZE:
            return users
    msg = (
        f"Realm {realm} still paging after {MAX_MEMBER_PAGES} pages; "
        "refusing to continue"
    )
    raise SyncError(msg)


def is_service_account(user: dict[str, Any]) -> bool:
    """Whether this realm user is a client's service account rather than a human.

    Keycloak stores a confidential client's service account as an ordinary
    realm user, so enumerating the realm returns them alongside people — this
    realm already has one for ``ol-opik-client`` and gains another for this
    job's own ``witan-token-sync`` client. Minting a human's interactive
    read/write token for them would hand every such client the Cedar rights of
    a person.

    ``serviceAccountClientId`` is where Keycloak records the owning client and
    is the authoritative signal; the username prefix is Keycloak's own naming
    convention for the same users and costs nothing to also check.
    """
    return bool(user.get("serviceAccountClientId")) or str(
        user.get("username", "")
    ).startswith("service-account-")


###############################################################################
#   Reconciliation                                                             #
###############################################################################


def reconcile(
    service_tokens: dict[str, str],
    current: dict[str, str],
    users: list[dict[str, Any]],
) -> dict[str, str]:
    """Compute the desired actor-token map.

    Disabled Keycloak users are skipped: leaving a token live for a disabled
    account would make "disable in Keycloak" a no-op for graph access, which is
    the one thing an operator disabling an account expects it not to be.

    Service-account users are skipped too — see :func:`is_service_account`. The
    non-human actors that DO get tokens come from the service map, which is
    declared in SOPS rather than discovered in Keycloak.
    """
    desired = dict(service_tokens)
    for user in users:
        sub = user.get("id")
        if not sub:
            msg = f"Keycloak returned a realm user with no id: {user!r}"
            raise SyncError(msg)
        if is_service_account(user):
            LOG.info(
                "skipping service account %s (client %s)",
                user.get("username", sub),
                user.get("serviceAccountClientId", "?"),
            )
            continue
        if not user.get("enabled", True):
            LOG.info("skipping disabled user %s", user.get("username", sub))
            continue
        actor_id = derive_actor_id(sub)
        if actor_id in service_tokens:
            # Can only happen if a service actor were named `act-<uuid>`, but
            # the collision would hand a human the service identity's token, so
            # it stops the run rather than resolving in either direction.
            msg = (
                f"Keycloak user {user.get('username', sub)} derives actor id "
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
    users = realm_users(keycloak_url, keycloak_realm, access_token)
    # A realm with no users at all is a failed lookup wearing the costume of a
    # legitimate result — a revoked role, a renamed realm, a silently-empty
    # page. Left unguarded it would retire every per-user token and report
    # success, which is exactly what the old group-does-not-exist check was
    # protecting against before the realm replaced the group as the audience.
    if not users:
        msg = (
            f"Keycloak realm {keycloak_realm} returned no users at all. That is "
            "a lookup failure, not an empty realm — this job will not retire "
            "every per-user token on the strength of it. Check that the "
            "witan-token-sync service account still holds the view-users role."
        )
        raise SyncError(msg)
    LOG.info("realm %s has %d user(s) before filtering", keycloak_realm, len(users))

    desired = reconcile(service_tokens, current, users)

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
