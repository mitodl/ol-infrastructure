"""Keycloak groups must end up with exactly the members their team has in OM.

Convergence has to hold in both directions: a member removed in OpenMetadata
leaves the Keycloak group on the next run, and a member added by hand in
Keycloak does not survive one. A team member with no Keycloak account is
reported, not fatal, and a failed read stops the run before any write.
"""

import itertools
import json

import pytest
import requests

from ol_infrastructure.substructure.open_metadata.scripts import (
    om_team_keycloak_sync,
)
from ol_infrastructure.substructure.open_metadata.scripts.om_team_keycloak_sync import (
    GroupPlan,
    Keycloak,
    SyncError,
    plan_sync,
    sync,
)


class FakeOpenMetadata:
    def __init__(self, teams: dict[str, set[str]]) -> None:
        self.teams = teams

    def member_emails(self, team: str) -> frozenset[str]:
        if team not in self.teams:
            msg = f"404 for team {team}"
            raise SyncError(msg)
        return frozenset(self.teams[team])


class FakeKeycloak:
    """The children of one parent group, and the realm's users."""

    def __init__(
        self, users: dict[str, str], groups: dict[str, set[str]] | None = None
    ) -> None:
        self.users = users  # email -> user id
        self._ids = (f"group-{n}" for n in itertools.count())
        self.groups: dict[str, tuple[str, set[str]]] = {}  # id -> (name, members)
        for name, members in (groups or {}).items():
            self.groups[next(self._ids)] = (name, set(members))
        self.writes: list[tuple[str, ...]] = []

    def members_by_name(self) -> dict[str, set[str]]:
        return dict(self.groups.values())

    def child_groups(self) -> dict[str, str]:
        return {name: group_id for group_id, (name, _) in self.groups.items()}

    def group_members(self, group_id: str) -> dict[str, str]:
        return {user_id: f"label-{user_id}" for user_id in self.groups[group_id][1]}

    def user_id_by_email(self, email: str) -> str | None:
        return self.users.get(email)

    def create_child_group(self, name: str) -> str:
        group_id = next(self._ids)
        self.groups[group_id] = (name, set())
        self.writes.append(("create", name))
        return group_id

    def delete_group(self, group_id: str) -> None:
        self.writes.append(("delete", self.groups.pop(group_id)[0]))

    def add_member(self, group_id: str, user_id: str) -> None:
        self.groups[group_id][1].add(user_id)
        self.writes.append(("add", self.groups[group_id][0], user_id))

    def remove_member(self, group_id: str, user_id: str) -> None:
        self.groups[group_id][1].remove(user_id)
        self.writes.append(("remove", self.groups[group_id][0], user_id))


USERS = {"alice@mit.edu": "u-alice", "bob@mit.edu": "u-bob", "carol@mit.edu": "u-carol"}


def test_plan_adds_and_removes_to_match_the_team():
    plan = plan_sync(
        desired={"analytics": frozenset({"alice@mit.edu", "bob@mit.edu"})},
        keycloak_ids_by_email=USERS,
        current={"analytics": frozenset({"u-bob", "u-carol"})},
    )

    assert plan.groups == (
        GroupPlan(
            team="analytics",
            create=False,
            add=frozenset({"u-alice"}),
            remove=frozenset({"u-carol"}),
            unmatched=frozenset(),
        ),
    )
    assert plan.delete == ()


def test_plan_is_empty_when_keycloak_already_mirrors_om():
    plan = plan_sync(
        desired={"analytics": frozenset({"alice@mit.edu"})},
        keycloak_ids_by_email=USERS,
        current={"analytics": frozenset({"u-alice"})},
    )

    (group,) = plan.groups
    assert not group.create
    assert group.add == group.remove == frozenset()
    assert plan.delete == ()


def test_plan_creates_missing_groups_and_deletes_undeclared_ones():
    plan = plan_sync(
        desired={"data-platform": frozenset({"alice@mit.edu"})},
        keycloak_ids_by_email=USERS,
        current={"retired-team": frozenset({"u-bob"})},
    )

    (group,) = plan.groups
    assert group.create
    assert group.add == {"u-alice"}
    assert plan.delete == ("retired-team",)


def test_plan_reports_members_without_a_keycloak_account():
    plan = plan_sync(
        desired={"analytics": frozenset({"alice@mit.edu", "nobody@example.com"})},
        keycloak_ids_by_email=USERS,
        current={"analytics": frozenset()},
    )

    (group,) = plan.groups
    assert group.add == {"u-alice"}
    assert group.unmatched == {"nobody@example.com"}


def test_sync_converges_and_a_second_run_writes_nothing():
    om = FakeOpenMetadata(
        {"data-platform": {"alice@mit.edu"}, "analytics": {"bob@mit.edu"}}
    )
    keycloak = FakeKeycloak(USERS)

    sync(["data-platform", "analytics"], om, keycloak)
    assert keycloak.members_by_name() == {
        "data-platform": {"u-alice"},
        "analytics": {"u-bob"},
    }

    keycloak.writes.clear()
    sync(["data-platform", "analytics"], om, keycloak)
    assert keycloak.writes == []


def test_removal_in_om_and_hand_edits_in_keycloak_are_both_undone():
    om = FakeOpenMetadata({"analytics": {"alice@mit.edu"}})
    # bob was removed from the team in OM; carol was added to the group by hand.
    keycloak = FakeKeycloak(USERS, {"analytics": {"u-alice", "u-bob", "u-carol"}})

    sync(["analytics"], om, keycloak)

    assert keycloak.members_by_name() == {"analytics": {"u-alice"}}


def test_an_emptied_team_empties_its_group_without_deleting_it():
    om = FakeOpenMetadata({"analytics": set()})
    keycloak = FakeKeycloak(USERS, {"analytics": {"u-alice"}})

    sync(["analytics"], om, keycloak)

    assert keycloak.members_by_name() == {"analytics": set()}


def test_a_member_without_an_account_is_skipped_not_fatal(capsys):
    om = FakeOpenMetadata({"analytics": {"alice@mit.edu", "nobody@example.com"}})
    keycloak = FakeKeycloak(USERS)

    plan = sync(["analytics"], om, keycloak)

    assert keycloak.members_by_name() == {"analytics": {"u-alice"}}
    assert plan.groups[0].unmatched == {"nobody@example.com"}
    assert "nobody@example.com has no Keycloak account" in capsys.readouterr().out


def test_a_failed_read_stops_the_run_before_any_write():
    # "analytics" is declared but OM has no such team.
    om = FakeOpenMetadata({"data-platform": {"alice@mit.edu"}})
    keycloak = FakeKeycloak(USERS, {"retired-team": {"u-bob"}})

    with pytest.raises(SyncError):
        sync(["data-platform", "analytics"], om, keycloak)

    assert keycloak.writes == []


def _response(status: int, body: object, headers: dict[str, str] | None = None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers.update(headers or {})
    return response


def test_create_child_group_reads_the_id_keycloak_returns(monkeypatch):
    # Keycloak 26.7.4 GroupResource.addChild answers a new child with 201, a
    # Location header, and the new GroupRepresentation as the JSON body.
    monkeypatch.setattr(
        om_team_keycloak_sync.requests,
        "post",
        lambda *_args, **_kwargs: _response(200, {"access_token": "token"}),
    )
    keycloak = Keycloak(
        realm_url="https://sso/realms/r",
        admin_url="https://sso/admin/realms/r",
        client_id="client",
        client_secret="secret",  # pragma: allowlist secret
        parent_group_id="parent",
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        return _response(
            201,
            {"id": "new-id", "name": "analytics", "path": "/parent/analytics"},
            {"Location": "https://sso/admin/realms/r/groups/new-id"},
        )

    monkeypatch.setattr(keycloak.session, "request", request)

    assert keycloak.create_child_group("analytics") == "new-id"
    assert calls == [
        (
            "POST",
            "https://sso/admin/realms/r/groups/parent/children",
            {"name": "analytics"},
        )
    ]
