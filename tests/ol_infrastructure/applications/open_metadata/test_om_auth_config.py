"""The auth-config Job must leave each settings row carrying the desired keys.

It merges only the keys that are missing or differ, so a row that already
agrees is not written, and every other key in the row is kept. adminPrincipals
is one of those keys: the team-keycloak-sync job reads it from the row, so a
stale list there demotes an admin the YAML still names.
"""

import importlib
import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

ADMINS = ["tmacey", "service-account-ol-openmetadata-provisioner"]
ENV = {
    "OM_SESSION_EXPIRY_SECONDS": "604800",
    "OM_MAX_ACTIVE_SESSIONS_PER_USER": "5",
    "OM_CLIENT_TYPE": "confidential",
    "OM_PROVIDER_NAME": "MIT Open Learning",
    "OM_USE_ROLES_FROM_PROVIDER": "true",
    "OM_ADMIN_PRINCIPALS": json.dumps(ADMINS),
}


class FakeCursor:
    """Serves one stored settings row per configtype and records UPDATEs."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self._fetched: Any = None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if sql.startswith("SELECT"):
            row = self.rows.get(params[0])
            self._fetched = None if row is None else (row,)
        else:
            self.updates.append((params[1], json.loads(params[0])))

    def fetchone(self) -> Any:
        return self._fetched


@pytest.fixture
def om_auth_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    # The script runs in the OpenMetadata ingestion image, which ships
    # psycopg2; this repo's environment does not.
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.errors = types.SimpleNamespace(UndefinedTable=Exception)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    module_name = "ol_infrastructure.applications.open_metadata.scripts.om_auth_config"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    yield importlib.import_module(module_name)
    # Drop the copy imported against the fake psycopg2.
    sys.modules.pop(module_name, None)


def test_desired_authentication_row_carries_the_provider_name(om_auth_config):
    assert om_auth_config.DESIRED["authenticationConfiguration"] == {
        "sessionExpiry": 604800,
        "maxActiveSessionsPerUser": 5,
        "clientType": "confidential",
        "providerName": "MIT Open Learning",
    }


def test_an_empty_provider_name_is_filled_in(om_auth_config, capsys):
    # The rows 2.0 seeded carry providerName "", which 2.1's login page rejects.
    desired = om_auth_config.DESIRED["authenticationConfiguration"]
    cursor = FakeCursor(
        {
            "authenticationConfiguration": {
                **desired,
                "providerName": "",
                "provider": "custom-oidc",
            }
        }
    )

    om_auth_config._reconcile_row(cursor, "authenticationConfiguration", desired)

    # jsonb || merges at the top level, so provider is kept.
    assert cursor.updates == [("authenticationConfiguration", desired)]
    assert "providerName: '' -> 'MIT Open Learning'" in capsys.readouterr().out


def test_desired_authorizer_row_carries_the_admin_list(om_auth_config):
    assert om_auth_config.DESIRED["authorizerConfiguration"] == {
        "useRolesFromProvider": True,
        "adminPrincipals": ADMINS,
    }


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(
            {"useRolesFromProvider": True, "adminPrincipals": ["tmacey"]},
            id="stale list",
        ),
        pytest.param({"useRolesFromProvider": True}, id="key missing"),
    ],
)
def test_a_row_without_the_admin_list_is_updated(om_auth_config, stored, capsys):
    desired = om_auth_config.DESIRED["authorizerConfiguration"]
    cursor = FakeCursor(
        {"authorizerConfiguration": {**stored, "principalDomain": "mit.edu"}}
    )

    om_auth_config._reconcile_row(cursor, "authorizerConfiguration", desired)

    # jsonb || merges at the top level, so principalDomain is kept.
    assert cursor.updates == [("authorizerConfiguration", desired)]
    assert "adminPrincipals" in capsys.readouterr().out


def test_a_row_that_already_agrees_is_not_written(om_auth_config, capsys):
    desired = om_auth_config.DESIRED["authorizerConfiguration"]
    cursor = FakeCursor(
        {
            "authorizerConfiguration": json.dumps(
                {**desired, "principalDomain": "mit.edu"}
            )
        }
    )

    om_auth_config._reconcile_row(cursor, "authorizerConfiguration", desired)

    assert cursor.updates == []
    assert capsys.readouterr().out.startswith("[ok]")


def test_a_missing_row_is_skipped(om_auth_config, capsys):
    cursor = FakeCursor({})

    om_auth_config._reconcile_row(
        cursor,
        "authorizerConfiguration",
        om_auth_config.DESIRED["authorizerConfiguration"],
    )

    assert cursor.updates == []
    assert "no authorizerConfiguration row yet" in capsys.readouterr().out
