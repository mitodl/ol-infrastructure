"""Each OpenMetadata user must end up with the roles a login would give them now.

OpenMetadata applies the Keycloak `roles` claim only at login, so the job has
to take away what a user's realm roles no longer grant and clear a stray
isAdmin. Admin principals, bots and users with no Keycloak account keep their
roles, a dry run writes nothing, and a failure in the role half never stops the
team half.
"""

import json
from typing import Any

import pytest
import requests

from ol_infrastructure.substructure.open_metadata.scripts import (
    om_team_keycloak_sync,
)
from ol_infrastructure.substructure.open_metadata.scripts.om_team_keycloak_sync import (
    OpenMetadata,
    OpenMetadataUser,
    SyncError,
    reconcile_roles,
)

ROLE_BY_REALM_ROLE = {
    "default-roles-ol-data-platform": "DataConsumer",
    "ol-platform-admin": "DataEngineer",
    "ol-data-engineer": "DataEngineer",
    "ol-researcher": "DataContributor",
}
ROLE_IDS = {
    "DataConsumer": "r-consumer",
    "DataEngineer": "r-engineer",
    "DataContributor": "r-contributor",
    "DataSteward": "r-steward",
}
ADMINS = frozenset({"admin", "service-account-ol-openmetadata-provisioner"})


class FakeKeycloak:
    def __init__(self, realm_roles: dict[str, set[str]]) -> None:
        self.realm_roles = realm_roles  # email -> effective realm roles

    def user_ids_by_email(self) -> dict[str, str]:
        return {email: f"kc-{email}" for email in self.realm_roles}

    def effective_realm_roles(self, user_id: str) -> frozenset[str]:
        return frozenset(self.realm_roles[user_id.removeprefix("kc-")])


class FakeOpenMetadata:
    def __init__(
        self,
        users: list[OpenMetadataUser],
        admin_principals: frozenset[str] = ADMINS,
    ) -> None:
        self.users = {user.id: user for user in users}
        self._admin_principals = admin_principals
        self.writes: list[tuple[str, ...]] = []

    def admin_principals(self) -> frozenset[str]:
        return self._admin_principals

    def role_ids(self) -> dict[str, str]:
        return dict(ROLE_IDS)

    def human_users(self) -> list[OpenMetadataUser]:
        return list(self.users.values())

    def patch_user(self, user_id: str, operations: list[dict[str, Any]]) -> None:
        user = self.users[user_id]
        names_by_id = {role_id: name for name, role_id in ROLE_IDS.items()}
        for operation in operations:
            if operation["path"] == "/roles":
                roles = frozenset(names_by_id[ref["id"]] for ref in operation["value"])
                self.writes.append(("roles", user.name, *sorted(roles)))
                user = OpenMetadataUser(
                    user.id, user.name, user.email, user.is_admin, roles
                )
            elif operation["path"] == "/isAdmin":
                self.writes.append(("isAdmin", user.name, str(operation["value"])))
                user = OpenMetadataUser(
                    user.id, user.name, user.email, operation["value"], user.roles
                )
        self.users[user_id] = user

    def roles_of(self, name: str) -> frozenset[str]:
        return next(user.roles for user in self.users.values() if user.name == name)


def _user(name: str, *roles: str, is_admin: bool = False) -> OpenMetadataUser:
    return OpenMetadataUser(
        id=f"om-{name}",
        name=name,
        email=f"{name}@mit.edu",
        is_admin=is_admin,
        roles=frozenset(roles),
    )


def _reconcile(om: FakeOpenMetadata, keycloak: FakeKeycloak, *, dry_run=False):
    return reconcile_roles(keycloak, om, ROLE_BY_REALM_ROLE, dry_run=dry_run)


def test_a_demoted_user_loses_the_role():
    om = FakeOpenMetadata([_user("alice", "DataConsumer", "DataEngineer")])
    # alice no longer holds ol-data-engineer.
    keycloak = FakeKeycloak({"alice@mit.edu": {"default-roles-ol-data-platform"}})

    _reconcile(om, keycloak)

    assert om.writes == [("roles", "alice", "DataConsumer")]
    assert om.roles_of("alice") == {"DataConsumer"}


def test_a_user_gains_the_roles_a_login_would_give():
    om = FakeOpenMetadata([_user("bob")])
    keycloak = FakeKeycloak(
        {"bob@mit.edu": {"default-roles-ol-data-platform", "ol-platform-admin"}}
    )

    _reconcile(om, keycloak)

    assert om.writes == [("roles", "bob", "DataConsumer", "DataEngineer")]


def test_a_user_already_in_line_gets_no_writes():
    om = FakeOpenMetadata([_user("carol", "DataConsumer", "DataContributor")])
    keycloak = FakeKeycloak(
        {"carol@mit.edu": {"default-roles-ol-data-platform", "ol-researcher"}}
    )

    plan = _reconcile(om, keycloak)

    assert om.writes == []
    assert plan.unchanged == 1


def test_a_second_run_writes_nothing():
    om = FakeOpenMetadata(
        [_user("alice", "DataEngineer"), _user("dave", is_admin=True)]
    )
    keycloak = FakeKeycloak(
        {
            "alice@mit.edu": {"default-roles-ol-data-platform"},
            "dave@mit.edu": {"default-roles-ol-data-platform"},
        }
    )

    _reconcile(om, keycloak)
    om.writes.clear()
    _reconcile(om, keycloak)

    assert om.writes == []


def test_a_stray_is_admin_is_cleared():
    om = FakeOpenMetadata([_user("dave", "DataConsumer", is_admin=True)])
    keycloak = FakeKeycloak({"dave@mit.edu": {"default-roles-ol-data-platform"}})

    _reconcile(om, keycloak)

    assert om.writes == [("isAdmin", "dave", "False")]


def test_admin_principals_are_left_alone():
    om = FakeOpenMetadata([_user("admin", "DataSteward", is_admin=True)])
    # Nothing in Keycloak grants DataSteward.
    keycloak = FakeKeycloak({"admin@mit.edu": {"default-roles-ol-data-platform"}})

    plan = _reconcile(om, keycloak)

    assert om.writes == []
    assert plan.admins == ("admin",)


def test_a_user_with_no_keycloak_account_keeps_roles_but_not_admin(capsys):
    om = FakeOpenMetadata([_user("erin", "DataEngineer", is_admin=True)])

    plan = _reconcile(om, FakeKeycloak({}))

    assert om.writes == [("isAdmin", "erin", "False")]
    assert plan.missing == ("erin",)
    assert "erin: no Keycloak account" in capsys.readouterr().out


def test_a_role_openmetadata_lacks_is_dropped_like_at_login(capsys):
    om = FakeOpenMetadata([_user("frank", "DataConsumer")])
    keycloak = FakeKeycloak(
        {"frank@mit.edu": {"default-roles-ol-data-platform", "ol-analyst"}}
    )

    plan = reconcile_roles(
        keycloak,
        om,
        {**ROLE_BY_REALM_ROLE, "ol-analyst": "Analyst"},
        dry_run=False,
    )

    assert om.writes == []
    assert plan.unknown_roles == {"Analyst"}
    assert "role Analyst" in capsys.readouterr().out


def test_a_dry_run_writes_nothing_but_logs_each_change(capsys):
    om = FakeOpenMetadata(
        [_user("alice", "DataEngineer"), _user("dave", is_admin=True)]
    )
    keycloak = FakeKeycloak(
        {
            "alice@mit.edu": {"default-roles-ol-data-platform"},
            "dave@mit.edu": {"default-roles-ol-data-platform"},
        }
    )

    plan = _reconcile(om, keycloak, dry_run=True)

    assert om.writes == []
    assert len(plan.changes) == 2
    out = capsys.readouterr().out
    assert "alice: would set roles [DataEngineer] -> [DataConsumer]" in out
    assert "dave: would set roles [] -> [DataConsumer], isAdmin true -> false" in out


def test_no_admin_principals_stops_the_run_before_any_write():
    om = FakeOpenMetadata([_user("dave", is_admin=True)], admin_principals=frozenset())

    with pytest.raises(SyncError):
        _reconcile(om, FakeKeycloak({"dave@mit.edu": set()}))

    assert om.writes == []


def _response(body: object) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(body).encode()
    return response


Call = tuple[str, str, dict[str, Any]]


def _recording_client(monkeypatch, body: object) -> tuple[OpenMetadata, list[Call]]:
    client = OpenMetadata("http://openmetadata:8585/api", "token")
    calls: list[Call] = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _response(body)

    monkeypatch.setattr(client.session, "request", request)
    return client, calls


def test_human_users_asks_the_server_to_leave_out_bots(monkeypatch):
    client, calls = _recording_client(
        monkeypatch,
        {
            "data": [
                {
                    "id": "u1",
                    "name": "alice",
                    "email": "Alice@MIT.edu",
                    "isAdmin": False,
                    "roles": [{"id": "r-consumer", "name": "DataConsumer"}],
                }
            ],
            "paging": {},
        },
    )

    assert client.human_users() == [
        OpenMetadataUser(
            id="u1",
            name="alice",
            email="alice@mit.edu",
            is_admin=False,
            roles=frozenset({"DataConsumer"}),
        )
    ]
    ((method, url, kwargs),) = calls
    assert (method, url) == ("GET", "http://openmetadata:8585/api/v1/users")
    assert kwargs["params"]["isBot"] == "false"
    assert kwargs["params"]["fields"] == "roles"


MAIN_ENV = {
    "OM_SERVER_URL": "http://openmetadata:8585/api",
    "OM_BOT_JWT_TOKEN": "bot-token",
    "OM_TEAMS": '["analytics"]',
    "OM_ROLES_DRY_RUN": "false",
    "OM_ROLE_BY_REALM_ROLE": json.dumps(ROLE_BY_REALM_ROLE),
    "KEYCLOAK_REALM_URL": "https://sso/realms/r",
    "KEYCLOAK_ADMIN_URL": "https://sso/admin/realms/r",
    "KEYCLOAK_CLIENT_ID": "team-sync",
    "KEYCLOAK_CLIENT_SECRET": "team-sync-secret",  # pragma: allowlist secret
    "KEYCLOAK_PARENT_GROUP_ID": "parent",
    "OM_ADMIN_CLIENT_ID": "provisioner",
    "OM_ADMIN_CLIENT_SECRET": "provisioner-secret",  # pragma: allowlist secret
}


@pytest.fixture
def main_env(monkeypatch) -> list[str]:
    """Run main() against stand-ins, recording which halves ran."""
    for name, value in MAIN_ENV.items():
        monkeypatch.setenv(name, value)
    ran: list[str] = []
    module = om_team_keycloak_sync
    monkeypatch.setattr(module, "Keycloak", lambda **_kwargs: object())
    monkeypatch.setattr(module, "client_credentials_token", lambda *_args: "token")

    def sync(*_args):
        ran.append("teams")
        return module.SyncPlan(groups=(), delete=())

    monkeypatch.setattr(module, "sync", sync)
    return ran


def test_a_missing_provisioner_secret_fails_only_the_role_half(
    monkeypatch, main_env, capsys
):
    # The secret references are optional, so the variables are simply unset.
    monkeypatch.delenv("OM_ADMIN_CLIENT_ID")
    monkeypatch.delenv("OM_ADMIN_CLIENT_SECRET")

    with pytest.raises(SystemExit) as exit_info:
        om_team_keycloak_sync.main()

    assert exit_info.value.code == 1
    assert main_env == ["teams"]
    err = capsys.readouterr().err
    assert "[fail] role reconcile: SyncError: OM_ADMIN_CLIENT_ID is unset" in err
    assert "om-provisioner" in err


def test_an_unexpected_error_in_the_role_half_is_reported_not_raised(
    monkeypatch, main_env, capsys
):
    # What frozenset(None) raises if adminPrincipals comes back null.
    def reconcile_roles(*_args, **_kwargs):
        msg = "'NoneType' object is not iterable"
        raise TypeError(msg)

    monkeypatch.setattr(om_team_keycloak_sync, "reconcile_roles", reconcile_roles)

    with pytest.raises(SystemExit) as exit_info:
        om_team_keycloak_sync.main()

    assert exit_info.value.code == 1
    assert main_env == ["teams"]
    assert "[fail] role reconcile: TypeError" in capsys.readouterr().err
