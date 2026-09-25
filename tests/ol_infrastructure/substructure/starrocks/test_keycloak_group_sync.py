"""The group file must list effective role holders by the principal StarRocks sees.

StarRocks matches the group file against principal_field=starrocks_username, which
Keycloak fills from the saml_uid attribute. Listing Keycloak usernames, or only
users with a direct client-role mapping, leaves the file empty or unmatchable, and
permitted_groups then refuses every OIDC login.
"""

from collections.abc import Callable
from typing import Any

import pytest

from ol_infrastructure.substructure.starrocks import keycloak_group_sync

User = dict[str, Any]

ADMIN = "https://sso.example.com/admin/realms/ol-data-platform"
CLIENT = "client-uuid"


def _user(user_id: str, saml_uid: str | None = None, *, enabled: bool = True) -> User:
    attributes = {} if saml_uid is None else {"saml_uid": [saml_uid]}
    return {
        "id": user_id,
        "username": f"{user_id}@mit.edu",
        "enabled": enabled,
        "attributes": attributes,
    }


USERS = [
    _user("alice", "alice"),
    _user("bob", "bob"),
    _user("carol"),
    _user("dave", "dave"),
    _user("erin", "erin", enabled=False),
    _user("frank", "frank,ol_platform_admin"),
]
# Effective client roles, as /users/{id}/role-mappings/clients/{c}/composite
# returns them after expanding composite realm roles and groups.
EFFECTIVE_ROLES = {
    "alice": ["ol_data_analyst", "ol_researcher"],
    "bob": ["ol_data_engineer", "uma_protection"],
    "carol": ["ol_data_analyst"],
    "dave": [],
    "erin": ["ol_platform_admin"],
    "frank": ["ol_data_analyst"],
}


@pytest.fixture
def serve_users(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[User]], None]:
    """Return a function that serves the given users in place of the admin API."""

    def serve(users: list[User]) -> None:
        def api_get_all(url: str, _headers: dict[str, str]) -> list[Any]:
            assert url == f"{ADMIN}/users?briefRepresentation=false"
            return users

        def api_get(url: str, _headers: dict[str, str]) -> list[Any]:
            user_id = url.removeprefix(f"{ADMIN}/users/").split("/")[0]
            assert url.endswith(f"/role-mappings/clients/{CLIENT}/composite")
            return [{"name": name} for name in EFFECTIVE_ROLES[user_id]]

        monkeypatch.setattr(keycloak_group_sync, "_api_get_all", api_get_all)
        monkeypatch.setattr(keycloak_group_sync, "_api_get", api_get)

    return serve


def test_members_are_saml_uids_of_effective_holders(
    serve_users: Callable[[list[User]], None],
) -> None:
    """Roles held only through a composite are listed, keyed by saml_uid."""
    serve_users(USERS)
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert members["ol_data_analyst"] == {"alice"}
    assert members["ol_researcher"] == {"alice"}
    assert members["ol_data_engineer"] == {"bob"}
    assert set(members) == set(keycloak_group_sync._GOVERNANCE_ROLES)


def test_disabled_users_are_left_out(
    serve_users: Callable[[list[User]], None],
) -> None:
    """A disabled user keeps no StarRocks group through the file."""
    serve_users(USERS)
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert members["ol_platform_admin"] == set()


def test_unusable_saml_uid_is_skipped_not_written(
    serve_users: Callable[[list[User]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A holder with a missing or format-breaking saml_uid is skipped."""
    serve_users(USERS)
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    written = {uid for uids in members.values() for uid in uids}
    assert written == {"alice", "bob"}
    err = capsys.readouterr().err
    assert "carol@mit.edu" in err
    assert "frank@mit.edu" in err


def test_all_holders_missing_saml_uid_fails_instead_of_writing_empty(
    serve_users: Callable[[list[User]], None],
) -> None:
    """An admin API that hides saml_uid must not produce a silently empty file."""
    serve_users([_user("alice"), _user("bob")])

    with pytest.raises(SystemExit, match="none has a usable saml_uid"):
        keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})
