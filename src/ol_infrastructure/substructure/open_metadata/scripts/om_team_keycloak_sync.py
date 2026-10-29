"""Mirror OpenMetadata team membership into Keycloak groups.

Team membership is assigned by hand in the OpenMetadata UI. This job makes the
``ol-data-platform`` realm hold one group per team, with exactly the members the
team has in OpenMetadata. It only writes to Keycloak; nothing flows back.

The teams are the ones ``access_control.py`` declares in ``TEAMS``; the stack
passes their names in ``OM_TEAMS``. Each becomes a child of the parent
group named by ``KEYCLOAK_PARENT_GROUP_ID``, which the keycloak substructure
stack owns. This job owns every child of that parent: it creates a missing one,
and deletes one whose team is no longer declared, so a dropped team does not
keep members who can no longer be removed in OpenMetadata.

OpenMetadata users are matched to Keycloak users by email. A member with no
Keycloak account is reported and skipped, and the run still succeeds. Any API
error stops the run with a non-zero exit before the remaining writes.

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


def _log(message: str) -> None:
    print(message)  # noqa: T201


class OpenMetadata:
    """Reads team membership from the OpenMetadata API."""

    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(
            f"{self.server_url}{path}", params=params, timeout=HTTP_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()

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
        return frozenset(user["email"].lower() for user in self._team_users(team))

    def _team_users(self, team: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"team": team, "isBot": "false", "limit": OM_PAGE_SIZE}
        while True:
            page = self._get("/v1/users", params)
            yield from page["data"]
            after = page["paging"].get("after")
            if not after:
                return
            params["after"] = after


class Keycloak:
    """Manages the children of one group in a Keycloak realm."""

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
        token_response = requests.post(
            f"{realm_url.rstrip('/')}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        token_response.raise_for_status()
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            f"Bearer {token_response.json()['access_token']}"
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
    """Mirror every declared team; exit non-zero on any API error."""
    teams = json.loads(os.environ["OM_TEAMS"])
    source = OpenMetadata(os.environ["OM_SERVER_URL"], os.environ["OM_BOT_JWT_TOKEN"])
    target = Keycloak(
        realm_url=os.environ["KEYCLOAK_REALM_URL"],
        admin_url=os.environ["KEYCLOAK_ADMIN_URL"],
        client_id=os.environ["KEYCLOAK_CLIENT_ID"],
        client_secret=os.environ["KEYCLOAK_CLIENT_SECRET"],
        parent_group_id=os.environ["KEYCLOAK_PARENT_GROUP_ID"],
    )
    try:
        plan = sync(teams, source, target)
    except (SyncError, requests.RequestException) as exc:
        print(f"[fail] {exc}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
    unmatched = sum(len(group.unmatched) for group in plan.groups)
    _log(f"\nSync complete. {unmatched} team member(s) skipped with no account.")


if __name__ == "__main__":
    main()
