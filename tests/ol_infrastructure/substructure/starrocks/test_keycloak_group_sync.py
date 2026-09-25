"""The group file must list effective role holders by the principal StarRocks sees.

StarRocks matches the group file against principal_field=starrocks_username, which
Keycloak fills from the saml_uid attribute. Listing Keycloak usernames, or only
users with a direct client-role mapping, leaves the file empty or unmatchable, and
permitted_groups then refuses every OIDC login.
"""

from typing import Any

import pytest

from ol_infrastructure.substructure.starrocks import keycloak_group_sync

ADMIN = "https://sso.example.com/admin/realms/ol-data-platform"
CLIENT = "client-uuid"

USERS = [
    {"id": "u1", "username": "alice@mit.edu", "attributes": {"saml_uid": ["alice"]}},
    {"id": "u2", "username": "bob@mit.edu", "attributes": {"saml_uid": ["bob"]}},
    {"id": "u3", "username": "carol@mit.edu", "attributes": {}},
    {"id": "u4", "username": "dave@mit.edu"},
]
# Effective client roles, as /users/{id}/role-mappings/clients/{c}/composite
# returns them after expanding composite realm roles and groups.
EFFECTIVE_ROLES = {
    "u1": ["ol_data_analyst", "ol_researcher"],
    "u2": ["ol_data_engineer", "uma_protection"],
    "u3": ["ol_data_analyst"],
    "u4": [],
}


@pytest.fixture
def fake_keycloak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve USERS and EFFECTIVE_ROLES in place of the Keycloak admin API."""

    def api_get_all(url: str, _headers: dict[str, str]) -> list[Any]:
        assert url == f"{ADMIN}/users?briefRepresentation=false"
        return USERS

    def api_get(url: str, _headers: dict[str, str]) -> list[Any]:
        user_id = url.removeprefix(f"{ADMIN}/users/").split("/")[0]
        assert url.endswith(f"/role-mappings/clients/{CLIENT}/composite")
        return [{"name": name} for name in EFFECTIVE_ROLES[user_id]]

    monkeypatch.setattr(keycloak_group_sync, "_api_get_all", api_get_all)
    monkeypatch.setattr(keycloak_group_sync, "_api_get", api_get)


@pytest.mark.usefixtures("fake_keycloak")
def test_members_are_saml_uids_of_effective_holders() -> None:
    """Roles held only through a composite are listed, keyed by saml_uid."""
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert members["ol_data_analyst"] == {"alice"}
    assert members["ol_researcher"] == {"alice"}
    assert members["ol_data_engineer"] == {"bob"}
    assert members["ol_platform_admin"] == set()


@pytest.mark.usefixtures("fake_keycloak")
def test_holder_without_saml_uid_is_skipped_not_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A holder with no saml_uid is reported, never written as an email."""
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert not any("@" in uid for uids in members.values() for uid in uids)
    assert "carol@mit.edu" in capsys.readouterr().err


@pytest.mark.usefixtures("fake_keycloak")
def test_non_governance_roles_are_ignored() -> None:
    """Only the six governance roles become groups."""
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert set(members) == set(keycloak_group_sync._GOVERNANCE_ROLES)
