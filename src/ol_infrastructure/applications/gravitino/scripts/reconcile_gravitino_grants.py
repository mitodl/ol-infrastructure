"""Converge Gravitino's metalake, catalog, roles, grants and users on desired state.

Gravitino has no policy file: who may read what is REST API state in its entity
store. This script is what makes the JSON document Pulumi renders (from
``lib/data_lake_access.py``) the source of truth for it. See
docs/plans/gravitino-authorization-spec.md, A7-A10. Each run:

1. Ensures the metalake and the catalog exist, and that the catalog carries the
   desired properties.
2. Ensures a group and a role exist for every governance role.
3. Replaces each managed role's privileges with the desired ones, so a privilege
   removed from the desired state, or added by hand, does not survive a run.
4. Grants each managed role to the group of the same name and revokes it from
   every other group and from every user.
5. Adds to the metalake every Keycloak user who effectively holds a governance
   role. Gravitino denies a user who is not in the metalake even when their
   groups hold roles, and has no create-on-first-login.
6. Sets the owner of every ``ol_warehouse_<env>_*`` schema to the engineering
   group, so ownership does not follow whoever created the schema.

Step 5 grants nothing. Group membership comes from the ``role_keys`` claim of
the user's own token, so a user this has not added yet is denied rather than
over-granted, and removing a Keycloak role takes effect on the user's next token
whether or not this has run. Users are never removed: that would also detach
whatever they own.

It authenticates twice. The client certificate gets it through the management
port's TLS handshake, and the ``ol-gravitino-admin`` access token makes it the
service admin. The same token carries the realm-management roles used to read
Keycloak.

Stdlib only: it runs on a stock ``python:*-slim`` image from a ConfigMap. Exits
non-zero on any API error, which is the alert signal (``WorkloadJobFailed*``).
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REQUEST_TIMEOUT_SECONDS = 30
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
# Gravitino answers 409 for several conditions; this code is the "already
# exists" one (ErrorConstants.ALREADY_EXISTS_CODE).
ALREADY_EXISTS_CODE = 1004
KEYCLOAK_PAGE_SIZE = 100
# Refresh the token this long before it expires. The realm issues 5-minute
# access tokens and a run can outlast one.
TOKEN_REFRESH_MARGIN_SECONDS = 60

# The only privileges and object types desired state may name. Everything else is
# refused outright. That keeps out the job privileges, which are code execution
# in the Gravitino pod (the job system runs shell templates in the server
# process), and CREATE_ROLE and the MANAGE_* privileges, which let a holder
# change grants outside this reconciler.
ALLOWED_PRIVILEGES = frozenset(
    {
        "USE_CATALOG",
        "USE_SCHEMA",
        "CREATE_SCHEMA",
        "SELECT_TABLE",
        "MODIFY_TABLE",
        "CREATE_TABLE",
    }
)
ALLOWED_OBJECT_TYPES = frozenset({"catalog", "schema", "table"})
# DENY does not cascade between these two, and MODIFY_TABLE includes reading, so
# denying only one hides nothing.
PAIRED_DENY_PRIVILEGES = frozenset({"SELECT_TABLE", "MODIFY_TABLE"})
# Server-managed catalog property, present on every read.
SERVER_CATALOG_PROPERTIES = frozenset({"in-use"})
IMMUTABLE_CATALOG_PROPERTIES = frozenset({"catalog-backend", "io-impl"})

# A principal becomes an STS role session name when credentials are vended,
# which caps it at 64 characters less Gravitino's own prefix.
MAX_PRINCIPAL_LENGTH = 41
# The form keycloak_group_sync.py accepts for the StarRocks group file. A
# principal StarRocks would refuse must not be admitted here, or the two layers
# disagree about who a user is.
PRINCIPAL_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# What Iceberg's GlueCatalog accepts as a database name
# (IcebergToGlueConverter.GLUE_DB_PATTERN). Glue itself allows more (hyphens,
# angle brackets), and such a database is listed but can never be loaded:
# "Cannot convert namespace ... to Glue database name". That holds only while
# the catalog leaves glue.skip-name-validation unset.
GLUE_NAMESPACE_PATTERN = re.compile(r"[a-z0-9_]{1,252}")

Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]
PrivilegeSet = frozenset[tuple[str, str]]
ObjectKey = tuple[str, str]


class ReconcileError(RuntimeError):
    """An API call failed or the desired state is not safe to apply."""


def log(message: str) -> None:
    """Write one line to stdout."""
    print(message, flush=True)  # noqa: T201


def warn(message: str) -> None:
    """Write one warning line to stderr."""
    print(f"WARNING: {message}", file=sys.stderr, flush=True)  # noqa: T201


def _segment(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def normalize_objects(
    securable_objects: list[dict[str, Any]],
) -> dict[ObjectKey, PrivilegeSet]:
    """Index securable objects by (type, full name) with privileges as a set.

    Gravitino emits enum values in lower case and accepts any case, and returns
    privileges in no stable order, so both sides of a comparison go through here.
    """
    return {
        (securable_object["type"].lower(), securable_object["fullName"]): frozenset(
            (privilege["name"].upper(), privilege["condition"].upper())
            for privilege in securable_object["privileges"]
        )
        for securable_object in securable_objects
    }


def denormalize_objects(
    objects: dict[ObjectKey, PrivilegeSet],
) -> list[dict[str, Any]]:
    """Turn the normalized form back into request bodies, in a stable order."""
    return [
        {
            "type": object_type,
            "fullName": full_name,
            "privileges": [
                {"name": name, "condition": condition}
                for name, condition in sorted(privileges)
            ],
        }
        for (object_type, full_name), privileges in sorted(objects.items())
    ]


def validate_desired_roles(roles: dict[str, list[dict[str, Any]]]) -> None:
    """Refuse desired state that would hand out a dangerous privilege.

    :param roles: Role name to its desired securable objects.

    :raises ReconcileError: When a role names a privilege or object type outside
        the allowed sets, repeats a securable object, or denies only one of
        SELECT_TABLE and MODIFY_TABLE.
    """
    problems = []
    for role, securable_objects in roles.items():
        normalized = normalize_objects(securable_objects)
        if len(normalized) != len(securable_objects):
            problems.append(f"{role} lists the same securable object twice")
        for (object_type, full_name), privileges in normalized.items():
            where = f"{role} on {object_type} {full_name}"
            if not privileges:
                problems.append(f"{where} has no privileges")
            if object_type not in ALLOWED_OBJECT_TYPES:
                problems.append(f"{where}: object type is not allowed")
            forbidden = {name for name, _ in privileges} - ALLOWED_PRIVILEGES
            if forbidden:
                problems.append(f"{where} names {sorted(forbidden)}, not allowed")
            denied = {name for name, condition in privileges if condition == "DENY"}
            if denied & PAIRED_DENY_PRIVILEGES not in (set(), PAIRED_DENY_PRIVILEGES):
                problems.append(
                    f"{where} denies only one of {sorted(PAIRED_DENY_PRIVILEGES)}"
                )
    if problems:
        msg = "Refusing desired state: " + "; ".join(problems)
        raise ReconcileError(msg)


def usable_principal(user: dict[str, Any]) -> tuple[str, str | None]:
    """Return a Keycloak user's Gravitino principal and why it is unusable, if so.

    Gravitino resolves the principal from ``starrocks_username`` (which Keycloak
    fills from the ``saml_uid`` attribute) and falls back to
    ``preferred_username``. An ``@`` means ``saml_uid`` was missing for a human
    and the fallback was their email address: adding it would give one person two
    names.
    """
    saml_uid = (user.get("attributes", {}).get("saml_uid") or [""])[0]
    principal = saml_uid or user["username"]
    if "@" in principal:
        return principal, "contains '@' (no saml_uid, so the email was used)"
    if len(principal) > MAX_PRINCIPAL_LENGTH:
        return principal, f"is longer than {MAX_PRINCIPAL_LENGTH} characters"
    if not PRINCIPAL_PATTERN.fullmatch(principal):
        return principal, f"does not match {PRINCIPAL_PATTERN.pattern}"
    return principal, None


class Gravitino:
    """The subset of the Gravitino management API the reconciler drives."""

    def __init__(self, transport: Transport, metalake: str) -> None:
        """Bind the API to one metalake.

        :param transport: Sends one request and returns (status, parsed body).
        :param metalake: The metalake every call is scoped to.
        """
        self._transport = transport
        self.metalake = metalake
        self._base = f"/api/metalakes/{_segment(metalake)}"

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
        exists_ok: bool = False,
    ) -> dict[str, Any] | None:
        status, payload = self._transport(method, path, body)
        if payload.get("code") == 0:
            return payload
        if missing_ok and status == HTTP_NOT_FOUND:
            return None
        if (
            exists_ok
            and status == HTTP_CONFLICT
            and payload.get("code") == ALREADY_EXISTS_CODE
        ):
            return None
        msg = (
            f"{method} {path} returned {status}: "
            f"{payload.get('type')}: {payload.get('message')}"
        )
        raise ReconcileError(msg)

    def _require(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = self._call(method, path, body)
        assert payload is not None  # noqa: S101
        return payload

    def ensure_metalake(self) -> None:
        """Create the metalake unless it exists.

        Creating it makes the caller its owner and a member. A GET cannot tell a
        missing metalake from one the caller is not a member of (both are 403),
        so this creates and treats "already exists" as success.
        """
        created = self._call(
            "POST",
            "/api/metalakes",
            {
                "name": self.metalake,
                "comment": "MIT Open Learning data platform",
                "properties": {},
            },
            exists_ok=True,
        )
        if created:
            log(f"Created metalake {self.metalake}")

    def ensure_catalog(self, catalog: dict[str, Any]) -> None:
        """Create the catalog, or set any desired property it does not carry."""
        path = f"{self._base}/catalogs/{_segment(catalog['name'])}"
        existing = self._call("GET", path, missing_ok=True)
        if existing is None:
            self._require("POST", f"{self._base}/catalogs", catalog)
            log(f"Created catalog {catalog['name']}")
            return
        # Fixed at creation, like the immutable properties below: no update
        # request can repair them.
        for field in ("type", "provider"):
            found = existing["catalog"].get(field, "")
            if found.lower() != catalog[field].lower():
                msg = (
                    f"Catalog {catalog['name']} has {field} {found!r}, expected "
                    f"{catalog[field]!r}; it has to be recreated by hand"
                )
                raise ReconcileError(msg)
        actual = existing["catalog"].get("properties", {})
        changed = {
            key: value
            for key, value in catalog["properties"].items()
            if actual.get(key, "") != value
        }
        immutable = changed.keys() & IMMUTABLE_CATALOG_PROPERTIES
        if immutable:
            msg = (
                f"Catalog {catalog['name']} differs on immutable properties "
                f"{sorted(immutable)}; it has to be recreated by hand"
            )
            raise ReconcileError(msg)
        if changed:
            self._require(
                "PUT",
                path,
                {
                    "updates": [
                        # Gravitino rejects setting a blank value on an existing
                        # catalog, though it accepts one at creation.
                        {"@type": "setProperty", "property": key, "value": value}
                        if value
                        else {"@type": "removeProperty", "property": key}
                        for key, value in sorted(changed.items())
                    ]
                },
            )
            log(f"Set catalog properties {sorted(changed)} on {catalog['name']}")
        extra = actual.keys() - catalog["properties"].keys() - SERVER_CATALOG_PROPERTIES
        if extra:
            warn(f"Catalog {catalog['name']} has unmanaged properties {sorted(extra)}")

    def list_schemas(self, catalog: str) -> list[str]:
        """Return every schema the backend catalog reports."""
        payload = self._require(
            "GET", f"{self._base}/catalogs/{_segment(catalog)}/schemas"
        )
        return sorted(identifier["name"] for identifier in payload["identifiers"])

    def schema_exists(self, catalog: str, schema: str) -> bool:
        """Load a schema, which also imports a backend schema Gravitino has not seen."""
        return (
            self._call(
                "GET",
                f"{self._base}/catalogs/{_segment(catalog)}/schemas/{_segment(schema)}",
                missing_ok=True,
            )
            is not None
        )

    def ensure_group(self, group: str) -> None:
        """Create a group unless it exists."""
        if self._call("POST", f"{self._base}/groups", {"name": group}, exists_ok=True):
            log(f"Created group {group}")

    def ensure_role(self, role: str) -> None:
        """Create an empty role unless it exists."""
        created = self._call(
            "POST",
            f"{self._base}/roles",
            {"name": role, "properties": {}, "securableObjects": []},
            exists_ok=True,
        )
        if created:
            log(f"Created role {role}")

    def list_roles(self) -> list[str]:
        """Return every role name in the metalake."""
        return self._require("GET", f"{self._base}/roles")["names"]

    def role_objects(self, role: str) -> dict[ObjectKey, PrivilegeSet]:
        """Return the securable objects a role currently holds."""
        payload = self._require("GET", f"{self._base}/roles/{_segment(role)}")
        return normalize_objects(payload["role"].get("securableObjects", []))

    def override_role(self, role: str, objects: dict[ObjectKey, PrivilegeSet]) -> None:
        """Replace everything a role holds with ``objects``."""
        self._require(
            "PUT",
            f"{self._base}/permissions/roles/{_segment(role)}/",
            {"overrides": denormalize_objects(objects)},
        )

    def principals_with_roles(self, kind: str) -> dict[str, set[str]]:
        """Map every user or group to the roles granted to it directly.

        :param kind: ``users`` or ``groups``.
        """
        payload = self._require("GET", f"{self._base}/{kind}?details=true")
        return {entry["name"]: set(entry.get("roles", [])) for entry in payload[kind]}

    def change_roles(
        self, kind: str, principal: str, action: str, roles: set[str]
    ) -> None:
        """Grant roles to, or revoke them from, a user or group.

        :param kind: ``users`` or ``groups``.
        :param action: ``grant`` or ``revoke``.
        """
        self._require(
            "PUT",
            f"{self._base}/permissions/{kind}/{_segment(principal)}/{action}",
            {"roleNames": sorted(roles)},
        )

    def schema_owner(self, catalog: str, schema: str) -> tuple[str, str] | None:
        """Return a schema's owner as (type, name), or None when it has none."""
        payload = self._require(
            "GET", f"{self._base}/owners/schema/{_segment(f'{catalog}.{schema}')}"
        )
        owner = payload.get("owner")
        return (owner["type"].lower(), owner["name"]) if owner else None

    def set_schema_owner_group(self, catalog: str, schema: str, group: str) -> None:
        """Make a group the owner of a schema."""
        self._require(
            "PUT",
            f"{self._base}/owners/schema/{_segment(f'{catalog}.{schema}')}",
            {"name": group, "type": "GROUP"},
        )

    def add_user(self, user: str) -> None:
        """Add a user to the metalake."""
        self._require("POST", f"{self._base}/users", {"name": user})


def reconcile_roles(
    gravitino: Gravitino, roles: dict[str, list[dict[str, Any]]]
) -> None:
    """Make every managed role hold exactly its desired privileges.

    A desired schema that does not exist yet (a layer dbt has not created) is
    skipped with a warning: Gravitino rejects a grant on a missing object, and a
    later run picks the schema up once it appears.
    """
    desired_schemas = {
        full_name
        for securable_objects in roles.values()
        for (object_type, full_name) in normalize_objects(securable_objects)
        if object_type == "schema"
    }
    missing = {
        full_name
        for full_name in sorted(desired_schemas)
        if not gravitino.schema_exists(*full_name.split(".", 1))
    }
    for full_name in sorted(missing):
        warn(f"Schema {full_name} does not exist; its grants are skipped this run")

    for role, securable_objects in roles.items():
        gravitino.ensure_group(role)
        gravitino.ensure_role(role)
        desired = {
            key: privileges
            for key, privileges in normalize_objects(securable_objects).items()
            if key[1] not in missing
        }
        actual = gravitino.role_objects(role)
        if actual == desired:
            continue
        gravitino.override_role(role, desired)
        for key in sorted(desired.keys() | actual.keys()):
            added = sorted(desired.get(key, frozenset()) - actual.get(key, frozenset()))
            removed = sorted(
                actual.get(key, frozenset()) - desired.get(key, frozenset())
            )
            if added or removed:
                log(f"Role {role} on {key[0]} {key[1]}: +{added} -{removed}")

    unmanaged = sorted(set(gravitino.list_roles()) - roles.keys())
    if unmanaged:
        warn(f"Roles not managed here, left alone: {unmanaged}")


def reconcile_role_bindings(gravitino: Gravitino, managed_roles: set[str]) -> None:
    """Bind each managed role to the group of the same name and to nothing else."""
    for group, held in gravitino.principals_with_roles("groups").items():
        wanted = {group} & managed_roles
        if wanted - held:
            gravitino.change_roles("groups", group, "grant", wanted - held)
            log(f"Granted role {group} to group {group}")
        stray = (held & managed_roles) - wanted
        if stray:
            gravitino.change_roles("groups", group, "revoke", stray)
            log(f"Revoked {sorted(stray)} from group {group}")
    for user, held in gravitino.principals_with_roles("users").items():
        stray = held & managed_roles
        if stray:
            gravitino.change_roles("users", user, "revoke", stray)
            log(f"Revoked {sorted(stray)} from user {user}: roles go to groups only")


def reconcile_schema_owners(
    gravitino: Gravitino, catalog: str, prefix: str, owner_group: str
) -> None:
    """Set the engineering group as owner of every schema under the prefix.

    Reasserted every run, because an owner can transfer ownership.
    """
    wanted = ("group", owner_group)
    failures = []
    for schema in gravitino.list_schemas(catalog):
        if not schema.startswith(prefix):
            continue
        if not GLUE_NAMESPACE_PATTERN.fullmatch(schema):
            # Nothing can reach it through the catalog, so it needs no owner.
            warn(f"Schema {schema!r} is not a name Iceberg can load; skipped")
            continue
        # One schema Gravitino cannot load must not leave every schema sorted
        # after it without its owner reasserted.
        try:
            # The list comes straight from the backend. Loading the schema
            # imports it, which the owner endpoints need.
            if not gravitino.schema_exists(catalog, schema):
                warn(f"Schema {schema} was listed but could not be loaded; skipped")
                continue
            owner = gravitino.schema_owner(catalog, schema)
            if owner != wanted:
                gravitino.set_schema_owner_group(catalog, schema, owner_group)
                log(f"Schema {schema}: owner {owner} -> {wanted}")
        except ReconcileError as err:
            failures.append(f"{schema}: {err}")
    if failures:
        msg = "Could not set the owner of: " + "; ".join(failures)
        raise ReconcileError(msg)


def reconcile_users(
    gravitino: Gravitino, holders: list[dict[str, Any]], reconciler_principal: str
) -> None:
    """Add every governance-role holder to the metalake.

    :param holders: Keycloak user representations of the enabled users who
        effectively hold a governance role.
    :param reconciler_principal: This job's own principal, which owns the
        metalake and holds no governance role.
    """
    existing = set(gravitino.principals_with_roles("users"))
    sources: dict[str, list[str]] = {}
    refused = 0
    for user in holders:
        principal, problem = usable_principal(user)
        if principal == reconciler_principal:
            problem = "is this reconciler's own principal, the metalake owner"
        if problem:
            warn(f"Not adding {user['username']}: principal {principal!r} {problem}")
            refused += 1
            continue
        sources.setdefault(principal, []).append(user["username"])
    wanted = set()
    for principal, usernames in sources.items():
        if len(usernames) > 1:
            # Two people under one name would share ownership and audit identity.
            warn(f"Not adding {principal!r}: {sorted(usernames)} all resolve to it")
            refused += len(usernames)
            continue
        wanted.add(principal)
    # The admin API omits attributes the realm's user profile does not expose. If
    # every holder was refused that is the likely cause, and it would otherwise
    # look like a quiet, successful run that admitted nobody.
    if holders and not wanted:
        msg = (
            f"{len(holders)} users hold governance roles and none has a usable "
            "principal; the warnings above give the reason for each. If every "
            "one is an '@' refusal, check that the realm's user profile exposes "
            "saml_uid"
        )
        raise ReconcileError(msg)
    for principal in sorted(wanted - existing):
        gravitino.add_user(principal)
        log(f"Added user {principal} to metalake {gravitino.metalake}")
    leftover = sorted(existing - wanted - {reconciler_principal})
    if leftover:
        warn(f"Metalake users holding no governance role, not removed: {leftover}")
    log(
        f"Users: {len(holders)} role holders, {len(wanted - existing)} added, "
        f"{refused} refused"
    )


def reconcile(
    gravitino: Gravitino,
    desired: dict[str, Any],
    find_holders: Callable[[], list[dict[str, Any]]],
    reconciler_principal: str,
) -> None:
    """Run every step against one Gravitino server.

    :param find_holders: Returns the Keycloak users who hold a governance role.
        Called only once roles and bindings have converged, so a Keycloak
        failure cannot hold up a revocation.
    """
    roles = desired["roles"]
    validate_desired_roles(roles)
    catalog = desired["catalog"]["name"]
    owner_group = desired["schema_owner_group"]
    if owner_group not in roles:
        msg = f"schema_owner_group {owner_group!r} is not a managed role"
        raise ReconcileError(msg)

    gravitino.ensure_metalake()
    gravitino.ensure_catalog(desired["catalog"])
    reconcile_roles(gravitino, roles)
    reconcile_role_bindings(gravitino, set(roles))
    # Before the owner pass, which touches every schema in the catalog and is the
    # step most likely to hit one it cannot load. That must not keep a new user
    # out.
    reconcile_users(gravitino, find_holders(), reconciler_principal)
    reconcile_schema_owners(
        gravitino, catalog, desired["owned_schema_prefix"], owner_group
    )


class KeycloakToken:
    """A client-credentials access token, fetched again shortly before it expires."""

    def __init__(self, issuer: str, client_id: str, client_secret: str) -> None:
        """Prepare the token request; nothing is fetched until ``get``."""
        self._issuer = issuer
        self._body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode()
        self._token = ""
        self._refresh_at = 0.0

    def get(self) -> str:
        """Return a token with at least the refresh margin left."""
        if time.monotonic() >= self._refresh_at:
            request = urllib.request.Request(  # noqa: S310
                f"{self._issuer}/protocol/openid-connect/token",
                data=self._body,
                method="POST",
            )
            try:
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=REQUEST_TIMEOUT_SECONDS
                ) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as err:
                msg = f"Keycloak token request failed: {err.code} {err.read().decode()}"
                raise ReconcileError(msg) from err
            self._token = payload["access_token"]
            self._refresh_at = (
                time.monotonic() + payload["expires_in"] - TOKEN_REFRESH_MARGIN_SECONDS
            )
        return self._token


def keycloak_get(url: str, token: KeycloakToken) -> Any:
    """GET a Keycloak admin API URL and return the parsed body."""
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Authorization": f"Bearer {token.get()}"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        msg = f"Keycloak GET {url} returned {err.code}: {err.read().decode()}"
        raise ReconcileError(msg) from err


def governance_role_holders(
    issuer: str,
    token: KeycloakToken,
    role_client_id: str,
    governance_roles: set[str],
    get: Callable[[str, KeycloakToken], Any] = keycloak_get,
) -> list[dict[str, Any]]:
    """Return every enabled Keycloak user who effectively holds a governance role.

    Effective, not direct: the realm hands these client roles out through
    composite realm roles, which ``/clients/{id}/roles/{role}/users`` does not
    expand. Each user's ``/role-mappings/clients/{id}/composite`` does, at the
    cost of one request per realm user.
    """
    base_url, _, realm = issuer.partition("/realms/")
    admin_base = f"{base_url}/admin/realms/{realm}"
    clients = get(
        f"{admin_base}/clients?clientId={urllib.parse.quote(role_client_id)}", token
    )
    if not clients:
        msg = f"Keycloak client {role_client_id} not found in realm {realm}"
        raise ReconcileError(msg)
    client_uuid = clients[0]["id"]

    holders = []
    scanned = 0
    first = 0
    while True:
        page = get(
            f"{admin_base}/users?briefRepresentation=false"
            f"&first={first}&max={KEYCLOAK_PAGE_SIZE}",
            token,
        )
        for user in page:
            scanned += 1
            if not user["enabled"]:
                continue
            mapped = get(
                f"{admin_base}/users/{user['id']}/role-mappings/clients/"
                f"{client_uuid}/composite",
                token,
            )
            if {role["name"] for role in mapped} & governance_roles:
                holders.append(user)
        if len(page) < KEYCLOAK_PAGE_SIZE:
            break
        first += KEYCLOAK_PAGE_SIZE
    # An empty result is legitimate (nobody holds a role yet) and is also what
    # an admin API that returns no users looks like. The count separates them.
    log(f"Keycloak: scanned {scanned} users, {len(holders)} hold a governance role")
    return holders


def gravitino_transport(
    base_uri: str, context: ssl.SSLContext, token: KeycloakToken
) -> Transport:
    """Build the function that sends one request to the management API."""

    def send(
        method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(  # noqa: S310
            f"{base_uri}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {token.get()}",
                "Accept": "application/vnd.gravitino.v1+json",
                # urllib would otherwise label a body as form-encoded.
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=REQUEST_TIMEOUT_SECONDS, context=context
            ) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as err:
            raw = err.read().decode()
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, {"type": "non-JSON response", "message": raw[:500]}

    return send


def main() -> int:
    """Read configuration from the environment and reconcile once."""
    desired = json.loads(Path(os.environ["DESIRED_STATE_FILE"]).read_text())
    issuer = os.environ["KEYCLOAK_ISSUER_URL"].rstrip("/")
    client_id = os.environ["KEYCLOAK_CLIENT_ID"]
    token = KeycloakToken(issuer, client_id, os.environ["KEYCLOAK_CLIENT_SECRET"])

    context = ssl.create_default_context(cafile=os.environ["GRAVITINO_CA_FILE"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        os.environ["GRAVITINO_CLIENT_CERT_FILE"],
        os.environ["GRAVITINO_CLIENT_KEY_FILE"],
    )

    try:
        reconcile(
            Gravitino(
                gravitino_transport(
                    os.environ["GRAVITINO_MANAGEMENT_URI"].rstrip("/"), context, token
                ),
                desired["metalake"],
            ),
            desired,
            lambda: governance_role_holders(
                issuer,
                token,
                desired["governance_role_client_id"],
                set(desired["roles"]),
            ),
            reconciler_principal=f"service-account-{client_id}",
        )
    except ReconcileError as err:
        print(f"FAIL: {err}", file=sys.stderr)  # noqa: T201
        return 1
    log("OK: Gravitino matches the desired state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
