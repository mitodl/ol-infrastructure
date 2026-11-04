"""The group file must list effective role holders by the principal StarRocks sees.

StarRocks matches the group file against principal_field=starrocks_username, which
Keycloak fills from the saml_uid attribute. Listing Keycloak usernames, or only
users with a direct client-role mapping, leaves the file empty or unmatchable, and
permitted_groups then refuses every OIDC login.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

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
    {**_user("gina"), "attributes": {"saml_uid": []}},
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
    "gina": ["ol_data_analyst"],
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
    assert "gina@mit.edu" in err


def test_all_holders_missing_saml_uid_fails_instead_of_writing_empty(
    serve_users: Callable[[list[User]], None],
) -> None:
    """An admin API that hides saml_uid must not produce a silently empty file."""
    serve_users([_user("alice"), _user("bob")])

    with pytest.raises(SystemExit, match="none has a usable saml_uid"):
        keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})


def test_counts_are_reported_so_an_empty_file_can_be_explained(
    serve_users: Callable[[list[User]], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The summary counts each stage of the filter separately."""
    monkeypatch.setitem(EFFECTIVE_ROLES, "dave", ["uma_protection"])
    serve_users(USERS)
    keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    out = capsys.readouterr().out
    assert "Scanned 7 users (6 enabled)" in out
    assert "6 hold an ol-starrocks-client role" in out
    assert "5 hold a governance role (3 skipped)" in out
    assert "ol_data_analyst=1" in out
    assert "ol_platform_admin=0" in out


@pytest.mark.parametrize(
    ("users", "expected"),
    [
        ([], "Scanned 0 users (0 enabled): 0 hold"),
        ([_user("dave", "dave")], "Scanned 1 users (1 enabled): 0 hold"),
    ],
)
def test_counts_are_reported_when_the_file_would_be_empty(
    serve_users: Callable[[list[User]], None],
    capsys: pytest.CaptureFixture[str],
    users: list[User],
    expected: str,
) -> None:
    """No visible users and no role holders print different lines."""
    serve_users(users)
    members = keycloak_group_sync.effective_role_members(ADMIN, CLIENT, {})

    assert not any(members.values())
    assert expected in capsys.readouterr().out


def test_group_file_lists_only_roles_with_holders() -> None:
    """Empty roles get no line, and holders are sorted so reruns compare equal."""
    members: dict[str, set[str]] = {
        role: set() for role in keycloak_group_sync._GOVERNANCE_ROLES
    }
    members["ol_researcher"] = {"zed", "alice"}
    members["ol_data_analyst"] = {"alice"}

    assert keycloak_group_sync.render_group_file(members) == (
        "ol_data_analyst:alice\nol_researcher:alice,zed"
    )


class FakeKubernetes:
    """Stand-in for the in-cluster API, holding one ConfigMap."""

    def __init__(self, data: dict[str, str] | None) -> None:
        """Start with the ConfigMap's data, or None for a ConfigMap with none."""
        self.data = data
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record the call and apply a PATCH as a merge into data."""
        self.calls.append((method, path, body))
        if method == "PATCH":
            assert body is not None
            self.data = {**(self.data or {}), **body["data"]}
        return {"data": self.data}


CONFIGMAP_PATH = (
    "/api/v1/namespaces/starrocks/configmaps/lakehouse-starrocks-oidc-groups"
)


@pytest.mark.parametrize("current", [None, {}, {"groups.txt": "ol_data_analyst:bob"}])
def test_in_cluster_write_patches_when_content_differs(
    monkeypatch: pytest.MonkeyPatch, current: dict[str, str] | None
) -> None:
    """A new file, or a ConfigMap with no data yet, is patched and reported."""
    fake = FakeKubernetes(current)
    monkeypatch.setattr(keycloak_group_sync, "_k8s_request", fake.request)

    changed = keycloak_group_sync.write_configmap_in_cluster(
        "starrocks", "lakehouse-starrocks-oidc-groups", "ol_data_analyst:alice"
    )

    assert changed
    assert fake.calls[-1] == (
        "PATCH",
        CONFIGMAP_PATH,
        {"data": {"groups.txt": "ol_data_analyst:alice"}},
    )


def test_in_cluster_write_leaves_current_content_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged file is not written, so the reload step skips its wait."""
    fake = FakeKubernetes({"groups.txt": "ol_data_analyst:alice"})
    monkeypatch.setattr(keycloak_group_sync, "_k8s_request", fake.request)

    changed = keycloak_group_sync.write_configmap_in_cluster(
        "starrocks", "lakehouse-starrocks-oidc-groups", "ol_data_analyst:alice"
    )

    assert not changed
    assert [method for method, _, _ in fake.calls] == ["GET"]


class _TokenResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"access_token": "token"}'


@pytest.mark.parametrize("changed", [True, False])
def test_changed_file_marks_only_a_changed_configmap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, changed: bool
) -> None:
    """The marker the reload container waits on exists only after a write."""
    marker = tmp_path / "changed"
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://sso.example.com/realms/r")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "ol-starrocks-client")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        keycloak_group_sync.urllib.request, "urlopen", lambda *_: _TokenResponse()
    )
    monkeypatch.setattr(keycloak_group_sync, "_api_get", lambda *_: [{"id": CLIENT}])
    monkeypatch.setattr(
        keycloak_group_sync,
        "effective_role_members",
        lambda *_: {"ol_data_analyst": {"alice"}},
    )
    monkeypatch.setattr(keycloak_group_sync, "_GOVERNANCE_ROLES", ("ol_data_analyst",))
    monkeypatch.setattr(
        keycloak_group_sync, "write_configmap_in_cluster", lambda *_: changed
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "keycloak_group_sync.py",
            "--in-cluster",
            "--configmap=lakehouse-starrocks-oidc-groups",
            f"--changed-file={marker}",
        ],
    )

    keycloak_group_sync.main()

    assert marker.exists() is changed


def test_unreachable_kubernetes_api_exits_with_a_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A connection failure reads as one line, not a traceback."""
    (tmp_path / "token").write_text("token")
    monkeypatch.setattr(keycloak_group_sync, "_SERVICE_ACCOUNT_DIR", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "1")
    monkeypatch.setattr(
        keycloak_group_sync.ssl, "create_default_context", lambda **_: None
    )

    with pytest.raises(SystemExit, match="Kubernetes API unreachable"):
        keycloak_group_sync._k8s_request("GET", CONFIGMAP_PATH)


def test_in_cluster_write_refuses_to_empty_a_populated_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty result must fail the run, not go live and refuse every login."""
    fake = FakeKubernetes({"groups.txt": "ol_data_analyst:alice"})
    monkeypatch.setattr(keycloak_group_sync, "_k8s_request", fake.request)

    with pytest.raises(SystemExit, match="Refusing to replace the non-empty"):
        keycloak_group_sync.write_configmap_in_cluster(
            "starrocks", "lakehouse-starrocks-oidc-groups", ""
        )
    assert [method for method, _, _ in fake.calls] == ["GET"]
