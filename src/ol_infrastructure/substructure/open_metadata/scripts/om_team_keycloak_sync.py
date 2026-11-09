"""Keep OpenMetadata teams and Keycloak in step, in both directions.

Each run does two things, in this order.

Teams to Keycloak groups. Team membership is assigned by hand in the
OpenMetadata UI. This makes the ``ol-data-platform`` realm hold one group per
team, with exactly the members the team has in OpenMetadata. The teams are the
ones ``access_control.py`` declares in ``TEAMS``; the stack passes their names
in ``OM_TEAMS``. Each becomes a child of the parent group named by
``KEYCLOAK_PARENT_GROUP_ID``, which the keycloak substructure stack owns. This
job owns every child of that parent: it creates a missing one, and deletes one
whose team is no longer declared, so a dropped team does not keep members who
can no longer be removed in OpenMetadata.

Keycloak roles to OpenMetadata roles. OpenMetadata sets a user's roles from the
``roles`` claim only when they log in (AuthenticationCodeFlowHandler
syncRolesFromProvider in 2.0.7), so a user whose Keycloak roles shrink keeps
their OpenMetadata roles until their next login, and indefinitely if they never
log in again. This resets every human user's roles to what that login would
give them now, and clears ``isAdmin`` on anyone outside the server's
``adminPrincipals``. The roles a login gives are the client roles of
``ol-open_metadata-client`` that the user's effective realm roles are
composites of; the stack passes that mapping in ``OM_ROLE_BY_REALM_ROLE``
(OPEN_METADATA_ROLE_BY_REALM_ROLE plus the realm's default role, from
substructure/keycloak ol_data_platform.py). Like the login, a role OpenMetadata
does not have is dropped. A client role assigned straight to a user in Keycloak,
rather than through a realm role, reaches the claim but not this mapping.

Users in ``adminPrincipals`` are left alone: login makes them admins again, and
an admin's roles grant nothing their admin flag does not. A user with no
Keycloak account is reported and keeps their roles, though a stray ``isAdmin``
is still cleared. When a user's roles shrink, their personal access tokens are
revoked. OpenMetadata 2.0.7 authorizes such a token with the roles stored on
the user (JwtFilter.getUserRolesFromClaims ignores the roles in a token it
issued), so this ends the API access the user set up under their old roles
rather than closing a way back to them. ``OM_ROLES_DRY_RUN`` true logs each of
these changes without making it.

The team half reads OpenMetadata as the ingestion bot. The role half needs an
admin, since roles, ``isAdmin`` and another user's tokens are admin-only, and
authenticates as the Keycloak service account that ``initialAdmins`` names
(``OM_ADMIN_CLIENT_ID``), which substructure/open_metadata provisions with.

OpenMetadata users are matched to Keycloak users by email. Any API error stops
that half with a non-zero exit before its remaining writes; the other half
still runs.

Runs as ``python -c`` in the ingestion-base image, which ships ``requests``.
"""

import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests

HTTP_TIMEOUT_SECONDS = 30
OM_PAGE_SIZE = 100
KEYCLOAK_PAGE_SIZE = 100
# A Group is the only OpenMetadata team type that holds users directly.
OM_GROUP_TEAM_TYPE = "Group"
JSON_PATCH_CONTENT_TYPE = "application/json-patch+json"


class SyncError(Exception):
    """A condition that must stop the run."""


@dataclass(frozen=True)
class GroupPlan:
    """The changes that bring one Keycloak group in line with its team.

    :param team: The OpenMetadata team name, which is also the group name.
    :param create: Whether the group has to be created first.
    :param add: Keycloak user ids to add.
    :param remove: Keycloak user ids to remove.
    :param unmatched: Emails of team members with no Keycloak account.
    """

    team: str
    create: bool
    add: frozenset[str]
    remove: frozenset[str]
    unmatched: frozenset[str]


@dataclass(frozen=True)
class SyncPlan:
    """Every change one run makes.

    :param groups: One entry per declared team, in name order.
    :param delete: Names of child groups whose team is no longer declared.
    """

    groups: tuple[GroupPlan, ...]
    delete: tuple[str, ...]


def plan_sync(
    desired: Mapping[str, frozenset[str]],
    keycloak_ids_by_email: Mapping[str, str],
    current: Mapping[str, frozenset[str]],
) -> SyncPlan:
    """Compute the changes that make Keycloak mirror OpenMetadata.

    :param desired: Each declared team's member emails, lower-cased.
    :param keycloak_ids_by_email: The Keycloak user id for each member email
        that has an account. Emails missing here are reported as unmatched.
    :param current: Each existing child group's member ids, by group name.
    :returns: The plan; applying it to an already mirrored realm changes nothing.
    :rtype: SyncPlan
    """
    groups = []
    for team in sorted(desired):
        emails = desired[team]
        wanted = frozenset(
            keycloak_ids_by_email[email]
            for email in emails
            if email in keycloak_ids_by_email
        )
        present = current.get(team, frozenset())
        groups.append(
            GroupPlan(
                team=team,
                create=team not in current,
                add=wanted - present,
                remove=present - wanted,
                unmatched=frozenset(
                    email for email in emails if email not in keycloak_ids_by_email
                ),
            )
        )
    return SyncPlan(
        groups=tuple(groups),
        delete=tuple(sorted(set(current) - set(desired))),
    )


@dataclass(frozen=True)
class OpenMetadataUser:
    """The fields of a human OpenMetadata user that the role half reads.

    :param id: The user's id.
    :param name: The user's name, which ``adminPrincipals`` lists.
    :param email: The user's email, lower-cased.
    :param is_admin: The user's ``isAdmin`` flag.
    :param roles: The names of the roles assigned to the user, not inherited.
    """

    id: str
    name: str
    email: str
    is_admin: bool
    roles: frozenset[str]


@dataclass(frozen=True)
class UserChange:
    """The changes that bring one OpenMetadata user in line with Keycloak.

    :param user: The user as OpenMetadata holds them.
    :param roles: The roles to set, or None to leave them.
    :param clear_admin: Whether to set ``isAdmin`` false.
    """

    user: OpenMetadataUser
    roles: frozenset[str] | None
    clear_admin: bool

    @property
    def removed_roles(self) -> frozenset[str]:
        """The roles the user loses, whose loss revokes their tokens."""
        return frozenset() if self.roles is None else self.user.roles - self.roles


@dataclass(frozen=True)
class RolePlan:
    """Every change one run of the role half makes.

    :param changes: One entry per user who needs a write, in name order.
    :param unchanged: How many Keycloak-matched users already match it.
    :param admins: Names of users left alone because they are admin principals.
    :param missing: Names of users with no Keycloak account.
    :param unknown_roles: Roles the mapping names that OpenMetadata lacks.
    """

    changes: tuple[UserChange, ...]
    unchanged: int
    admins: tuple[str, ...]
    missing: tuple[str, ...]
    unknown_roles: frozenset[str]


def plan_roles(
    users: Sequence[OpenMetadataUser],
    admin_principals: frozenset[str],
    realm_roles_by_email: Mapping[str, frozenset[str]],
    role_by_realm_role: Mapping[str, str],
    known_roles: frozenset[str],
) -> RolePlan:
    """Compute the changes that give each user the roles a login would.

    :param users: Every human OpenMetadata user.
    :param admin_principals: The server's ``adminPrincipals``.
    :param realm_roles_by_email: Each Keycloak-matched user's effective realm
        roles, by lower-cased email. A user missing here has no account.
    :param role_by_realm_role: The OpenMetadata role each realm role grants.
    :param known_roles: The names of the roles OpenMetadata has.
    :returns: The plan; applying it to users already in line changes nothing.
    :rtype: RolePlan
    """
    changes = []
    unchanged = 0
    admins = []
    missing = []
    unknown: set[str] = set()
    for user in sorted(users, key=lambda user: user.name):
        if user.name in admin_principals:
            admins.append(user.name)
            continue
        roles = None
        if user.email in realm_roles_by_email:
            granted = {
                role_by_realm_role[realm_role]
                for realm_role in realm_roles_by_email[user.email]
                if realm_role in role_by_realm_role
            }
            unknown |= granted - known_roles
            wanted = frozenset(granted & known_roles)
            roles = wanted if wanted != user.roles else None
        else:
            missing.append(user.name)
        if roles is not None or user.is_admin:
            changes.append(
                UserChange(user=user, roles=roles, clear_admin=user.is_admin)
            )
        elif user.email in realm_roles_by_email:
            unchanged += 1
    return RolePlan(
        changes=tuple(changes),
        unchanged=unchanged,
        admins=tuple(admins),
        missing=tuple(missing),
        unknown_roles=frozenset(unknown),
    )


class TeamSource(Protocol):
    """Where team membership is read from."""

    def member_emails(self, team: str) -> frozenset[str]:
        """Return the lower-cased emails of a team's human members."""


class GroupTarget(Protocol):
    """The Keycloak operations the sync needs."""

    def child_groups(self) -> dict[str, str]:
        """Return the parent group's children as ``{name: id}``."""

    def group_members(self, group_id: str) -> dict[str, str]:
        """Return a group's members as ``{user id: label}``."""

    def user_id_by_email(self, email: str) -> str | None:
        """Return the id of the user with this email, or None."""

    def create_child_group(self, name: str) -> str:
        """Create a child of the parent group and return its id."""

    def delete_group(self, group_id: str) -> None:
        """Delete a group."""

    def add_member(self, group_id: str, user_id: str) -> None:
        """Add a user to a group."""

    def remove_member(self, group_id: str, user_id: str) -> None:
        """Remove a user from a group."""


class RoleSource(Protocol):
    """The Keycloak reads the role half needs."""

    def user_ids_by_email(self) -> dict[str, str]:
        """Return every realm user's id by lower-cased email."""

    def effective_realm_roles(self, user_id: str) -> frozenset[str]:
        """Return a user's realm roles, composites and groups expanded."""


class RoleTarget(Protocol):
    """The OpenMetadata operations the role half needs."""

    def admin_principals(self) -> frozenset[str]:
        """Return the server's ``adminPrincipals``."""

    def role_ids(self) -> dict[str, str]:
        """Return every role's id by name."""

    def human_users(self) -> list[OpenMetadataUser]:
        """Return every user who is not a bot."""

    def patch_user(self, user_id: str, operations: list[dict[str, Any]]) -> None:
        """Apply a JSON Patch to a user."""

    def personal_access_token_count(self, user_name: str) -> int:
        """Return how many personal access tokens a user holds."""

    def revoke_personal_access_tokens(self, user_name: str) -> None:
        """Revoke every personal access token a user holds."""


def sync(teams: Sequence[str], source: TeamSource, target: GroupTarget) -> SyncPlan:
    """Read both sides, plan, and apply the plan to Keycloak.

    :param teams: The declared team names.
    :param source: OpenMetadata, or a stand-in.
    :param target: Keycloak, or a stand-in.
    :returns: The plan that was applied.
    :rtype: SyncPlan
    """
    desired = {team: source.member_emails(team) for team in teams}
    group_ids = target.child_groups()
    labels: dict[str, str] = {}
    current: dict[str, frozenset[str]] = {}
    for name, group_id in group_ids.items():
        members = target.group_members(group_id)
        labels.update(members)
        current[name] = frozenset(members)
    keycloak_ids_by_email: dict[str, str] = {}
    for email in sorted(set().union(*desired.values())):
        user_id = target.user_id_by_email(email)
        if user_id is not None:
            keycloak_ids_by_email[email] = user_id
            labels[user_id] = email

    plan = plan_sync(desired, keycloak_ids_by_email, current)
    for name in plan.delete:
        target.delete_group(group_ids[name])
        _log(f"[done] group {name}: deleted, its team is no longer declared")
    for group in plan.groups:
        _apply_group(group, group_ids, labels, target)
    return plan


def reconcile_roles(
    source: RoleSource,
    target: RoleTarget,
    role_by_realm_role: Mapping[str, str],
    *,
    dry_run: bool,
) -> RolePlan:
    """Read both sides, plan, and apply the plan to OpenMetadata.

    :param source: Keycloak, or a stand-in.
    :param target: OpenMetadata as an admin, or a stand-in.
    :param role_by_realm_role: The OpenMetadata role each realm role grants.
    :param dry_run: Log each change without making it.
    :returns: The plan that was applied, or would have been.
    :rtype: RolePlan
    :raises SyncError: If the server reports no admin principals, since every
        admin would then lose ``isAdmin``.
    """
    admin_principals = target.admin_principals()
    if not admin_principals:
        msg = "the server reports no adminPrincipals"
        raise SyncError(msg)
    role_ids = target.role_ids()
    users = target.human_users()
    keycloak_ids = source.user_ids_by_email()
    realm_roles_by_email = {
        user.email: source.effective_realm_roles(keycloak_ids[user.email])
        for user in users
        if user.name not in admin_principals and user.email in keycloak_ids
    }

    plan = plan_roles(
        users,
        admin_principals,
        realm_roles_by_email,
        role_by_realm_role,
        frozenset(role_ids),
    )
    for role in sorted(plan.unknown_roles):
        _log(f"[warn] role {role}: granted by a realm role but not in OpenMetadata")
    for name in plan.missing:
        _log(f"[skip] user {name}: no Keycloak account, roles left as they are")
    for change in plan.changes:
        _apply_user(change, role_ids, target, dry_run=dry_run)
    return plan


def _apply_group(
    group: GroupPlan,
    group_ids: dict[str, str],
    labels: Mapping[str, str],
    target: GroupTarget,
) -> None:
    if group.create:
        group_ids[group.team] = target.create_child_group(group.team)
        _log(f"[done] group {group.team}: created")
    group_id = group_ids[group.team]
    for user_id in sorted(group.add):
        target.add_member(group_id, user_id)
        _log(f"[done] group {group.team}: added {labels[user_id]}")
    for user_id in sorted(group.remove):
        target.remove_member(group_id, user_id)
        _log(f"[done] group {group.team}: removed {labels[user_id]}")
    for email in sorted(group.unmatched):
        _log(f"[skip] group {group.team}: {email} has no Keycloak account")
    if not (group.create or group.add or group.remove):
        _log(f"[ok]   group {group.team}: already mirrors the team")


def _apply_user(
    change: UserChange,
    role_ids: Mapping[str, str],
    target: RoleTarget,
    *,
    dry_run: bool,
) -> None:
    user = change.user
    operations: list[dict[str, Any]] = []
    described = []
    if change.roles is not None:
        operations.append(
            {
                "op": "add",
                "path": "/roles",
                "value": [
                    {"id": role_ids[role], "type": "role"}
                    for role in sorted(change.roles)
                ],
            }
        )
        described.append(f"roles {_names(user.roles)} -> {_names(change.roles)}")
    if change.clear_admin:
        operations.append({"op": "replace", "path": "/isAdmin", "value": False})
        described.append("isAdmin true -> false")
    tag, verb = ("[dry]  ", "would set") if dry_run else ("[done] ", "set")
    if not dry_run:
        target.patch_user(user.id, operations)
    _log(f"{tag}user {user.name}: {verb} {', '.join(described)}")

    if change.removed_roles:
        tokens = target.personal_access_token_count(user.name)
        if tokens:
            if not dry_run:
                target.revoke_personal_access_tokens(user.name)
            action = "would revoke" if dry_run else "revoked"
            _log(
                f"{tag}user {user.name}: {action} {tokens} personal access "
                f"token(s), as they lost {_names(change.removed_roles)}"
            )


def _names(roles: frozenset[str]) -> str:
    return "[" + ", ".join(sorted(roles)) + "]"


def _log(message: str) -> None:
    print(message)  # noqa: T201


def client_credentials_token(realm_url: str, client_id: str, secret: str) -> str:
    """Return an access token for a confidential client's service account.

    :param realm_url: The realm's base URL, ``<keycloak>/realms/<realm>``.
    :param client_id: The client's id.
    :param secret: The client's secret.
    """
    response = requests.post(
        f"{realm_url.rstrip('/')}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["access_token"]


class OpenMetadata:
    """The OpenMetadata API calls of both halves."""

    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method, f"{self.server_url}{path}", timeout=HTTP_TIMEOUT_SECONDS, **kwargs
        )
        response.raise_for_status()
        return response

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def _paged(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        params = {**params, "limit": OM_PAGE_SIZE}
        while True:
            page = self._get(path, params)
            yield from page["data"]
            after = page["paging"].get("after")
            if not after:
                return
            params["after"] = after

    def member_emails(self, team: str) -> frozenset[str]:
        """Return the lower-cased emails of a team's members, leaving out bots.

        :param team: The team name.
        :raises SyncError: If the team is not a ``Group``, since only a Group
            holds users and the stack creates nothing else.
        :raises requests.HTTPError: On any non-2xx response, including a 404
            for a team that does not exist yet.
        """
        team_type = self._get(f"/v1/teams/name/{team}")["teamType"]
        if team_type != OM_GROUP_TEAM_TYPE:
            msg = f"team {team} has teamType {team_type!r}, expected 'Group'"
            raise SyncError(msg)
        return frozenset(
            user["email"].lower()
            for user in self._paged("/v1/users", {"team": team, "isBot": "false"})
        )

    def admin_principals(self) -> frozenset[str]:
        # The settings endpoints refuse authorizerConfiguration
        # (SystemResource.getSettingByName); /security/config is admin-only.
        config = self._get("/v1/system/security/config")
        return frozenset(config["authorizerConfiguration"]["adminPrincipals"])

    def role_ids(self) -> dict[str, str]:
        return {role["name"]: role["id"] for role in self._paged("/v1/roles", {})}

    def human_users(self) -> list[OpenMetadataUser]:
        return [
            OpenMetadataUser(
                id=user["id"],
                name=user["name"],
                email=user["email"].lower(),
                is_admin=bool(user.get("isAdmin")),
                roles=frozenset(role["name"] for role in user.get("roles", [])),
            )
            for user in self._paged("/v1/users", {"isBot": "false", "fields": "roles"})
        ]

    def patch_user(self, user_id: str, operations: list[dict[str, Any]]) -> None:
        self._request(
            "PATCH",
            f"/v1/users/{user_id}",
            json=operations,
            headers={"Content-Type": JSON_PATCH_CONTENT_TYPE},
        )

    def personal_access_token_count(self, user_name: str) -> int:
        tokens = self._get("/v1/users/security/token", {"username": user_name})
        return len(tokens["data"])

    def revoke_personal_access_tokens(self, user_name: str) -> None:
        self._request(
            "PUT",
            "/v1/users/security/token/revoke",
            params={"username": user_name, "removeAll": "true"},
            json={},
        )


class Keycloak:
    """Manages the children of one group in a Keycloak realm, and reads users."""

    def __init__(
        self,
        realm_url: str,
        admin_url: str,
        client_id: str,
        client_secret: str,
        parent_group_id: str,
    ) -> None:
        self.admin_url = admin_url.rstrip("/")
        self.parent_group_id = parent_group_id
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            f"Bearer {client_credentials_token(realm_url, client_id, client_secret)}"
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method, f"{self.admin_url}{path}", timeout=HTTP_TIMEOUT_SECONDS, **kwargs
        )
        response.raise_for_status()
        return response

    def _paged(self, path: str) -> Iterator[dict[str, Any]]:
        first = 0
        while True:
            page = self._request(
                "GET",
                path,
                params={
                    "first": first,
                    "max": KEYCLOAK_PAGE_SIZE,
                    "briefRepresentation": "true",
                },
            ).json()
            yield from page
            if len(page) < KEYCLOAK_PAGE_SIZE:
                return
            first += KEYCLOAK_PAGE_SIZE

    def child_groups(self) -> dict[str, str]:
        return {
            group["name"]: group["id"]
            for group in self._paged(f"/groups/{self.parent_group_id}/children")
        }

    def group_members(self, group_id: str) -> dict[str, str]:
        return {
            user["id"]: user.get("email") or user["username"]
            for user in self._paged(f"/groups/{group_id}/members")
        }

    def user_id_by_email(self, email: str) -> str | None:
        users = self._request(
            "GET",
            "/users",
            params={"email": email, "exact": "true", "briefRepresentation": "true"},
        ).json()
        matches = [user for user in users if user.get("email", "").lower() == email]
        if len(matches) > 1:
            msg = f"{len(matches)} Keycloak users have the email {email}"
            raise SyncError(msg)
        return matches[0]["id"] if matches else None

    def user_ids_by_email(self) -> dict[str, str]:
        return {
            user["email"].lower(): user["id"]
            for user in self._paged("/users")
            if user.get("email")
        }

    # Needs only view on the user (RoleMapperResource.getCompositeRealmRoleMappings
    # in Keycloak 26.7.4), which manage-users covers. The client-role variant
    # would also need view-clients.
    def effective_realm_roles(self, user_id: str) -> frozenset[str]:
        roles = self._request(
            "GET", f"/users/{user_id}/role-mappings/realm/composite"
        ).json()
        return frozenset(role["name"] for role in roles)

    def create_child_group(self, name: str) -> str:
        return self._request(
            "POST", f"/groups/{self.parent_group_id}/children", json={"name": name}
        ).json()["id"]

    def delete_group(self, group_id: str) -> None:
        self._request("DELETE", f"/groups/{group_id}")

    def add_member(self, group_id: str, user_id: str) -> None:
        self._request("PUT", f"/users/{user_id}/groups/{group_id}")

    def remove_member(self, group_id: str, user_id: str) -> None:
        self._request("DELETE", f"/users/{user_id}/groups/{group_id}")


def main() -> None:
    """Mirror every declared team, then reconcile every user's roles.

    Exits non-zero if either half hits an API error.
    """
    server_url = os.environ["OM_SERVER_URL"]
    realm_url = os.environ["KEYCLOAK_REALM_URL"]
    keycloak = Keycloak(
        realm_url=realm_url,
        admin_url=os.environ["KEYCLOAK_ADMIN_URL"],
        client_id=os.environ["KEYCLOAK_CLIENT_ID"],
        client_secret=os.environ["KEYCLOAK_CLIENT_SECRET"],
        parent_group_id=os.environ["KEYCLOAK_PARENT_GROUP_ID"],
    )
    failed = False

    try:
        plan = sync(
            json.loads(os.environ["OM_TEAMS"]),
            OpenMetadata(server_url, os.environ["OM_BOT_JWT_TOKEN"]),
            keycloak,
        )
    except (SyncError, requests.RequestException) as exc:
        print(f"[fail] team sync: {exc}", file=sys.stderr)  # noqa: T201
        failed = True
    else:
        unmatched = sum(len(group.unmatched) for group in plan.groups)
        _log(f"\nTeam sync complete. {unmatched} member(s) skipped with no account.\n")

    dry_run = json.loads(os.environ["OM_ROLES_DRY_RUN"])
    try:
        admin = OpenMetadata(
            server_url,
            client_credentials_token(
                realm_url,
                os.environ["OM_ADMIN_CLIENT_ID"],
                os.environ["OM_ADMIN_CLIENT_SECRET"],
            ),
        )
        roles = reconcile_roles(
            keycloak,
            admin,
            json.loads(os.environ["OM_ROLE_BY_REALM_ROLE"]),
            dry_run=dry_run,
        )
    except (SyncError, requests.RequestException) as exc:
        print(f"[fail] role reconcile: {exc}", file=sys.stderr)  # noqa: T201
        failed = True
    else:
        _log(
            f"\nRole reconcile complete{' (dry run)' if dry_run else ''}. "
            f"{len(roles.changes)} user(s) {'to change' if dry_run else 'changed'}, "
            f"{roles.unchanged} unchanged, "
            f"{len(roles.admins)} admin principal(s) skipped, "
            f"{len(roles.missing)} with no Keycloak account."
        )

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
