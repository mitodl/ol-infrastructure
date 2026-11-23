"""Create/adopt/update paths of the remote provider against a fake admin backend."""

from typing import Any

import pytest

from ol_infrastructure.providers.keycloak_scim import remote_provider
from ol_infrastructure.providers.keycloak_scim.remote_provider import (
    SCIM_USER_SCHEMA,
    ScimRemoteProviderProvider,
    ScimRemoteProviderSpec,
    ScimUserAttribute,
)

KEYCLOAK_URL = "https://sso.example.test"
BACKEND = f"{KEYCLOAK_URL}/realms/master/scim/admin/backend/scim/v2"
PROVIDERS = f"{BACKEND}/RemoteScimProviderConfig"
ATTRIBUTES = f"{BACKEND}/KeycloakResourceSchemaAttributes"


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self) -> Any:
        return self.payload


class FakeBackend:
    """Just enough of the scim-for-keycloak admin backend to follow the calls."""

    def __init__(self) -> None:
        self.providers: dict[str, dict[str, Any]] = {}
        self.schemas: list[dict[str, Any]] = []
        self.attributes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def add_provider(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = {**body, "id": f"p{len(self.providers)}"}
        self.providers[provider["id"]] = provider
        return provider

    def load_defaults(self, provider_id: str) -> None:
        schema_id = f"s-{provider_id}"
        self.schemas.append(
            {
                "id": schema_id,
                "schemaId": SCIM_USER_SCHEMA,
                "scimRemoteProviderId": provider_id,
            }
        )
        self.attributes[f"a-{provider_id}-title"] = {
            "id": f"a-{provider_id}-title",
            "schemaId": schema_id,
            "name": "title",
            "type": "string",
        }
        # The plugin resets the capability flags to RFC defaults.
        self.providers[provider_id]["bulkSupported"] = False

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("GET", url, None))
        listings = {
            PROVIDERS: list(self.providers.values()),
            f"{BACKEND}/KeycloakResourceSchemas": self.schemas,
            ATTRIBUTES: list(self.attributes.values()),
        }
        if url in listings:
            return FakeResponse(
                {"Resources": listings[url], "totalResults": len(listings[url])}
            )
        return FakeResponse(self.providers[url.rsplit("/", 1)[1]])

    def post(self, url: str, json: dict[str, Any], timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("POST", url, json))
        if url == PROVIDERS:
            # useDefaultScimConfiguration is ignored on POST.
            return FakeResponse(self.add_provider(json), 201)
        attribute = {**json, "id": f"a{len(self.attributes)}"}
        self.attributes[attribute["id"]] = attribute
        return FakeResponse(attribute, 201)

    def put(self, url: str, json: dict[str, Any], timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("PUT", url, json))
        resource_id = url.rsplit("/", 1)[1]
        if url.startswith(ATTRIBUTES):
            self.attributes[resource_id] = json
            return FakeResponse(json)
        stored = {
            key: value
            for key, value in json.items()
            if key not in ("useDefaultScimConfiguration", "reloadConfiguration")
        }
        self.providers[resource_id] = {**stored, "id": resource_id}
        if json.get("useDefaultScimConfiguration"):
            self.load_defaults(resource_id)
        return FakeResponse(self.providers[resource_id])


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr(remote_provider, "_session", lambda _props: fake)
    return fake


def _props(*, user_attributes: list[ScimUserAttribute] | None) -> dict[str, Any]:
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-mitxonline-scim",
        base_url="http://mitxonline-webapp:8073/scim/v2",
        enabled=True,
        bulk_supported=True,
        user_attributes=user_attributes,
    )
    return {
        "keycloak_url": KEYCLOAK_URL,
        "realm": "olapps",
        "client_id": "scim-config-manager",
        "client_secret": "secret",  # pragma: allowlist secret
        "spec": spec.to_scim(),
        "authentication": None,
        "user_attributes": spec.user_attributes_to_scim(),
    }


FULL_NAME = ScimUserAttribute(
    name="fullName", type="string", custom_attribute_name="fullName"
)


def _commands(backend: FakeBackend) -> list[bool]:
    return [
        bool(body.get("useDefaultScimConfiguration"))
        for method, url, body in backend.calls
        if method == "PUT" and url.startswith(PROVIDERS)
    ]


def test_new_provider_gets_default_resource_types_then_declared_flags(
    backend: FakeBackend,
) -> None:
    result = ScimRemoteProviderProvider().create(_props(user_attributes=[FULL_NAME]))

    # Defaults are loaded with a PUT (the POST ignores the command), then a
    # plain PUT puts back the flags the defaults reset.
    assert _commands(backend) == [True, False]
    assert backend.providers[result.id]["bulkSupported"] is True
    added = [a for a in backend.attributes.values() if a["name"] == "fullName"]
    assert len(added) == 1
    assert added[0]["schemaId"] == f"s-{result.id}"
    assert added[0]["type"] == "string"
    assert added[0]["customAttributeName"] == "fullName"


def test_adopted_provider_keeps_its_configuration(backend: FakeBackend) -> None:
    """A live provider is never reset to defaults, which deletes its schema setup."""
    existing = backend.add_provider(
        {
            "name": "ol-mitxonline-scim",
            "assignedRealm": {"realmName": "olapps"},
            "bulkSupported": True,
        }
    )
    backend.load_defaults(existing["id"])
    backend.calls.clear()

    ScimRemoteProviderProvider().create(_props(user_attributes=None))

    assert _commands(backend) == [False]
    assert not [url for _, url, _ in backend.calls if url.startswith(ATTRIBUTES)]


def test_matching_attribute_is_left_alone_and_a_changed_one_corrected(
    backend: FakeBackend,
) -> None:
    olds = _props(user_attributes=[FULL_NAME])
    created = ScimRemoteProviderProvider().create(olds)
    backend.calls.clear()

    ScimRemoteProviderProvider().update(
        created.id, olds, _props(user_attributes=[FULL_NAME])
    )
    assert not [c for c in backend.calls if c[0] != "GET" and ATTRIBUTES in c[1]]

    renamed = ScimUserAttribute(
        name="fullName", type="string", custom_attribute_name="displayName"
    )
    ScimRemoteProviderProvider().update(
        created.id, olds, _props(user_attributes=[renamed])
    )
    writes = [c for c in backend.calls if c[0] != "GET" and ATTRIBUTES in c[1]]
    assert [method for method, _, _ in writes] == ["PUT"]
    assert writes[0][2]["customAttributeName"] == "displayName"


def test_user_attributes_without_a_user_schema_fail_loudly(
    backend: FakeBackend,
) -> None:
    existing = backend.add_provider(
        {"name": "ol-mitxonline-scim", "assignedRealm": {"realmName": "olapps"}}
    )

    with pytest.raises(RuntimeError, match="no User schema"):
        ScimRemoteProviderProvider().update(
            existing["id"], {}, _props(user_attributes=[FULL_NAME])
        )


def test_diff_sees_a_user_attribute_change() -> None:
    olds = _props(user_attributes=[FULL_NAME])
    news = _props(user_attributes=None)

    assert ScimRemoteProviderProvider().diff("p0", olds, news).changes is True
    assert ScimRemoteProviderProvider().diff("p0", olds, olds).changes is False
