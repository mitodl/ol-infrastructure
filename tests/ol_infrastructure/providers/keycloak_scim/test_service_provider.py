"""A realm's inbound SCIM server config against a fake admin backend."""

import copy
from typing import Any

import pytest

from ol_infrastructure.providers.keycloak_scim import service_provider
from ol_infrastructure.providers.keycloak_scim.service_provider import (
    READ_ONLY_ATTRIBUTES,
    SERVICE_PROVIDER_CONFIG_SCHEMA,
    ScimServiceProviderConfigProvider,
    ScimServiceProviderSpec,
)

KEYCLOAK_URL = "https://sso.example.test"
CONFIGS = (
    f"{KEYCLOAK_URL}/realms/master/scim/admin/backend/scim/v2"
    "/KeycloakServiceProviderConfig"
)
REALM_INFOS = READ_ONLY_ATTRIBUTES[1]


def _default_config(realm: str) -> dict[str, Any]:
    # The shape the plugin (4.1.5) lists for a realm whose config was never
    # saved: no id, inactive, with RFC capability defaults.
    return {
        "schemas": [SERVICE_PROVIDER_CONFIG_SCHEMA, REALM_INFOS],
        "realm": {"id": f"{realm}-uuid", "name": realm},
        "active": False,
        "limitClientAccess": False,
        "bulk": {"supported": True, "maxOperations": 15},
        REALM_INFOS: {
            "availableClients": [
                {"id": f"{realm}-admin-cli-uuid", "clientId": "admin-cli"},
                {"id": f"{realm}-mxo-uuid", "clientId": "ol-mitxonline-client"},
            ]
        },
        "meta": {"resourceType": "ServiceProviderBackendConfig"},
    }


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self) -> Any:
        return self.payload


class FakeBackend:
    """The plugin's KeycloakServiceProviderConfig endpoints, one config per realm."""

    def __init__(self, realms: list[str]) -> None:
        self.configs = {realm: _default_config(realm) for realm in realms}
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("GET", url, None))
        assert url == CONFIGS
        listed = list(copy.deepcopy(self.configs).values())
        return FakeResponse({"Resources": listed, "totalResults": len(listed)})

    def post(self, url: str, json: dict[str, Any], timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("POST", url, json))
        assert url == CONFIGS
        realm = json["realm"]["name"]
        self.configs[realm] = {**json, "id": f"{realm}-config"}
        return FakeResponse(self.configs[realm], 201)

    def put(self, url: str, json: dict[str, Any], timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("PUT", url, json))
        realm = json["realm"]["name"]
        assert url == f"{CONFIGS}/{self.configs[realm]['id']}"
        self.configs[realm] = {**json, "id": self.configs[realm]["id"]}
        return FakeResponse(self.configs[realm])

    def delete(self, url: str, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("DELETE", url, None))
        return FakeResponse({"status": "501"}, 501)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend(["master", "olapps"])
    monkeypatch.setattr(service_provider, "_session", lambda _props: fake)
    return fake


def _props(
    realm: str = "olapps",
    *,
    active: bool = True,
    authorized_clients: list[str] | None = None,
) -> dict[str, Any]:
    spec = ScimServiceProviderSpec(realm=realm, active=active)
    return {
        "keycloak_url": KEYCLOAK_URL,
        "realm": realm,
        "client_id": "scim-config-manager",
        "client_secret": "s",  # pragma: allowlist secret
        "spec": spec.to_scim(),
        "authorized_clients": authorized_clients,
    }


def test_authorized_clients_are_sent_with_their_internal_ids(backend):
    ScimServiceProviderConfigProvider().create(
        _props(authorized_clients=["ol-mitxonline-client"])
    )

    assert backend.configs["olapps"]["authorizedClients"] == [
        {"id": "olapps-mxo-uuid", "clientId": "ol-mitxonline-client"}
    ]


def test_authorized_clients_replace_the_live_list(backend):
    backend.configs["olapps"].update(
        {
            "id": "olapps-config",
            "authorizedClients": [
                {"id": "olapps-admin-cli-uuid", "clientId": "admin-cli"}
            ],
        }
    )

    ScimServiceProviderConfigProvider().create(
        _props(authorized_clients=["ol-mitxonline-client"])
    )

    assert [c["clientId"] for c in backend.configs["olapps"]["authorizedClients"]] == [
        "ol-mitxonline-client"
    ]


def test_undeclared_authorized_clients_are_left_alone(backend):
    live = [{"id": "olapps-admin-cli-uuid", "clientId": "admin-cli"}]
    backend.configs["olapps"].update({"id": "olapps-config", "authorizedClients": live})
    provider = ScimServiceProviderConfigProvider()

    provider.create(_props())

    assert backend.configs["olapps"]["authorizedClients"] == live
    assert provider.read("olapps-config", _props()).outs["authorized_clients"] is None


@pytest.mark.usefixtures("backend")
def test_an_authorized_client_missing_from_the_realm_fails():
    with pytest.raises(RuntimeError, match=r"not found in realm olapps: \['nope'\]"):
        ScimServiceProviderConfigProvider().create(
            _props(authorized_clients=["ol-mitxonline-client", "nope"])
        )


def test_read_reports_authorized_clients_and_diff_ignores_their_order(backend):
    declared = ["ol-mitxonline-client", "admin-cli"]
    provider = ScimServiceProviderConfigProvider()
    provider.create(_props(authorized_clients=declared))
    backend.configs["olapps"]["authorizedClients"].reverse()

    outs = provider.read("olapps-config", _props(authorized_clients=declared)).outs

    assert outs["authorized_clients"] == ["admin-cli", "ol-mitxonline-client"]
    assert not provider.diff(
        "olapps-config", outs, _props(authorized_clients=declared)
    ).changes
    assert provider.diff(
        "olapps-config", outs, _props(authorized_clients=["admin-cli"])
    ).changes


def test_create_saves_an_unsaved_config_by_post(backend):
    result = ScimServiceProviderConfigProvider().create(_props())

    [post] = [call for call in backend.calls if call[0] == "POST"]
    body = post[2]
    assert result.id == "olapps-config"
    assert body["active"] is True
    assert body["realm"]["name"] == "olapps"
    # The rest of the default config goes back as listed, minus what the
    # plugin only reports.
    assert body["bulk"] == {"supported": True, "maxOperations": 15}
    assert body["schemas"] == [SERVICE_PROVIDER_CONFIG_SCHEMA]
    assert not set(READ_ONLY_ATTRIBUTES) & set(body)
    assert backend.configs["master"]["active"] is False


def test_create_updates_a_saved_config_by_put(backend):
    backend.configs["olapps"].update({"id": "olapps-config", "limitClientAccess": True})

    result = ScimServiceProviderConfigProvider().create(_props())

    assert result.id == "olapps-config"
    assert [call[0] for call in backend.calls] == ["GET", "PUT"]
    assert backend.configs["olapps"]["active"] is True
    assert backend.configs["olapps"]["limitClientAccess"] is True


@pytest.mark.usefixtures("backend")
def test_create_fails_for_a_realm_the_backend_does_not_list():
    with pytest.raises(RuntimeError, match="realm missing"):
        ScimServiceProviderConfigProvider().create(_props("missing"))


@pytest.mark.usefixtures("backend")
def test_read_reports_an_unsaved_config_as_gone():
    result = ScimServiceProviderConfigProvider().read("olapps-config", _props())

    assert result.id == ""


def test_read_reports_the_live_value_of_declared_attributes_only(backend):
    provider = ScimServiceProviderConfigProvider()
    provider.create(_props())
    backend.configs["olapps"]["active"] = False

    result = provider.read("olapps-config", _props())

    assert result.id == "olapps-config"
    assert result.outs["spec"] == {"active": False}
    assert provider.diff("olapps-config", result.outs, _props()).changes


def test_a_realm_change_replaces():
    diff = ScimServiceProviderConfigProvider().diff(
        "olapps-config", _props("olapps"), _props("master")
    )

    assert diff.changes
    assert diff.replaces == ["realm"]


def test_delete_leaves_the_config_in_place(backend):
    ScimServiceProviderConfigProvider().delete("olapps-config", _props())

    assert backend.calls == []
