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
from tests.ol_infrastructure.providers.keycloak_scim.fakes import FakeResponse

KEYCLOAK_URL = "https://sso.example.test"
BACKEND = f"{KEYCLOAK_URL}/realms/master/scim/admin/backend/scim/v2"
PROVIDERS = f"{BACKEND}/RemoteScimProviderConfig"
SCHEMAS = f"{BACKEND}/KeycloakResourceSchemas"
ATTRIBUTES = f"{BACKEND}/KeycloakResourceSchemaAttributes"


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
        # Like the plugin: drop the provider's schema configuration, load the
        # RFC schemas, and reset the capability flags.
        old = {
            s["id"] for s in self.schemas if s["scimRemoteProviderId"] == provider_id
        }
        self.schemas = [s for s in self.schemas if s["id"] not in old]
        self.attributes = {
            key: value
            for key, value in self.attributes.items()
            if value["schemaId"] not in old
        }
        schema_id = f"s-{provider_id}-{len(self.calls)}"
        self.schemas.append(
            {
                "id": schema_id,
                "schemaId": SCIM_USER_SCHEMA,
                "scimRemoteProviderId": provider_id,
            }
        )
        self.attributes[f"a-{schema_id}-title"] = {
            "id": f"a-{schema_id}-title",
            "schemaId": schema_id,
            "name": "title",
            "type": "string",
        }
        self.providers[provider_id]["bulkSupported"] = False

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("GET", url, None))
        listings = {
            PROVIDERS: list(self.providers.values()),
            SCHEMAS: self.schemas,
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

    def delete(self, url: str, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
        self.calls.append(("DELETE", url, None))
        self.attributes.pop(url.rsplit("/", 1)[1])
        return FakeResponse(None, 204)

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


FULL_NAME = ScimUserAttribute(
    name="fullName", type="string", custom_attribute_name="fullName"
)


def _props(*, user_attributes: list[ScimUserAttribute] | None) -> dict[str, Any]:
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-mitxonline-scim",
        base_url="http://mitxonline-webapp:8073/scim/v2",
        enabled=True,
        bulk_supported=True,
        user_attributes=user_attributes,
    )
    props = {
        "keycloak_url": KEYCLOAK_URL,
        "realm": "olapps",
        "client_id": "scim-config-manager",
        "client_secret": "secret",  # pragma: allowlist secret
        "spec": spec.to_scim(),
        "authentication": None,
        "user_attributes": spec.user_attributes_to_scim(),
    }
    # The engine drops None-valued inputs, so undeclared attributes are absent.
    if props["user_attributes"] is None:
        del props["user_attributes"]
    return props


def _default_loads(backend: FakeBackend) -> int:
    return sum(
        1
        for method, url, body in backend.calls
        if method == "PUT"
        and url.startswith(PROVIDERS)
        and body.get("useDefaultScimConfiguration")
    )


def _attribute_writes(backend: FakeBackend) -> list[tuple[str, str, Any]]:
    return [
        call for call in backend.calls if call[0] != "GET" and ATTRIBUTES in call[1]
    ]


def _existing_provider(backend: FakeBackend) -> dict[str, Any]:
    """Add a provider configured by hand: schemas loaded, flags set."""
    provider = backend.add_provider(
        {
            "name": "ol-mitxonline-scim",
            "assignedRealm": {"realmName": "olapps"},
            "bulkSupported": True,
        }
    )
    backend.load_defaults(provider["id"])
    backend.calls.clear()
    return provider


def test_new_provider_gets_default_resource_types_then_declared_flags(
    backend: FakeBackend,
) -> None:
    result = ScimRemoteProviderProvider().create(_props(user_attributes=[FULL_NAME]))

    assert _default_loads(backend) == 1
    # The defaults reset the flags; the plain PUT after puts the declared ones back.
    assert backend.providers[result.id]["bulkSupported"] is True
    added = [a for a in backend.attributes.values() if a["name"] == "fullName"]
    assert len(added) == 1
    assert added[0]["type"] == "string"
    assert added[0]["customAttributeName"] == "fullName"


def test_undeclared_provider_is_never_given_default_configuration(
    backend: FakeBackend,
) -> None:
    """Without the fullName mapping a default schema corrupts names, so stay inert."""
    result = ScimRemoteProviderProvider().create(_props(user_attributes=None))
    ScimRemoteProviderProvider().update(result.id, {}, _props(user_attributes=None))

    assert _default_loads(backend) == 0
    assert not backend.schemas
    assert not _attribute_writes(backend)


def test_empty_user_attributes_configure_the_provider_without_custom_ones(
    backend: FakeBackend,
) -> None:
    """An empty list still opts in: resource types load, nothing custom is added."""
    result = ScimRemoteProviderProvider().create(_props(user_attributes=[]))

    assert _default_loads(backend) == 1
    assert [s["scimRemoteProviderId"] for s in backend.schemas] == [result.id]
    assert not _attribute_writes(backend)


@pytest.mark.parametrize("user_attributes", [None, [], [FULL_NAME]])
def test_configured_provider_is_never_reset(
    backend: FakeBackend, user_attributes: list[ScimUserAttribute] | None
) -> None:
    """Adopting or updating a provider with schemas keeps its configuration."""
    existing = _existing_provider(backend)
    schemas_before = list(backend.schemas)

    ScimRemoteProviderProvider().create(_props(user_attributes=user_attributes))
    ScimRemoteProviderProvider().update(
        existing["id"], {}, _props(user_attributes=user_attributes)
    )

    assert _default_loads(backend) == 0
    assert backend.schemas == schemas_before
    assert any(a["name"] == "title" for a in backend.attributes.values())
    # A declared attribute is added to the configured schema without a reset.
    has_full_name = any(a["name"] == "fullName" for a in backend.attributes.values())
    assert has_full_name == bool(user_attributes)


def test_read_reports_live_mappings_so_refresh_sees_drift(
    backend: FakeBackend,
) -> None:
    """A mapping removed in the admin console shows as a diff after refresh."""
    props = _props(user_attributes=[FULL_NAME])
    created = ScimRemoteProviderProvider().create(props)

    unchanged = ScimRemoteProviderProvider().read(created.id, props).outs
    assert unchanged["user_attributes"] == props["user_attributes"]
    assert not ScimRemoteProviderProvider().diff(created.id, unchanged, props).changes

    full_name = next(
        k for k, a in backend.attributes.items() if a["name"] == "fullName"
    )
    del backend.attributes[full_name]
    drifted = ScimRemoteProviderProvider().read(created.id, props).outs

    assert drifted["user_attributes"] == []
    assert ScimRemoteProviderProvider().diff(created.id, drifted, props).changes


def test_read_reports_a_missing_user_schema(backend: FakeBackend) -> None:
    """A provider without schemas never reads as a valid empty mapping list."""
    bare = backend.add_provider(
        {"name": "ol-mitxonline-scim", "assignedRealm": {"realmName": "olapps"}}
    )
    props = _props(user_attributes=[])

    outs = ScimRemoteProviderProvider().read(bare["id"], props).outs

    assert outs["user_attributes"] == remote_provider.NO_USER_SCHEMA
    assert ScimRemoteProviderProvider().diff(bare["id"], outs, props).changes


def test_read_leaves_an_undeclared_schema_unmanaged(backend: FakeBackend) -> None:
    existing = _existing_provider(backend)

    outs = (
        ScimRemoteProviderProvider()
        .read(existing["id"], _props(user_attributes=None))
        .outs
    )

    assert "user_attributes" not in outs
    assert not [c for c in backend.calls if SCHEMAS in c[1] or ATTRIBUTES in c[1]]


def test_repairing_an_existing_bare_provider_warns_to_restart_keycloak(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin keeps skipping a provider it already loaded until it reloads."""
    warnings: list[str] = []
    monkeypatch.setattr(remote_provider.pulumi.log, "warn", warnings.append)

    ScimRemoteProviderProvider().create(_props(user_attributes=[]))
    assert not warnings

    backend.add_provider(
        {"name": "ol-learn-scim", "assignedRealm": {"realmName": "olapps"}}
    )
    learn = _props(user_attributes=[])
    learn["spec"] = {**learn["spec"], "name": "ol-learn-scim"}
    ScimRemoteProviderProvider().create(learn)

    assert len(warnings) == 1
    assert "Restart Keycloak" in warnings[0]


def test_provider_left_without_schemas_is_repaired_on_adopt(
    backend: FakeBackend,
) -> None:
    """A create that failed after its POST leaves a bare provider; next run fixes it."""
    backend.add_provider(
        {"name": "ol-mitxonline-scim", "assignedRealm": {"realmName": "olapps"}}
    )

    ScimRemoteProviderProvider().create(_props(user_attributes=[FULL_NAME]))

    assert _default_loads(backend) == 1
    assert any(a["name"] == "fullName" for a in backend.attributes.values())


def test_matching_attribute_is_left_alone_and_a_changed_one_corrected(
    backend: FakeBackend,
) -> None:
    olds = _props(user_attributes=[FULL_NAME])
    created = ScimRemoteProviderProvider().create(olds)
    backend.calls.clear()

    ScimRemoteProviderProvider().update(
        created.id, olds, _props(user_attributes=[FULL_NAME])
    )
    assert not _attribute_writes(backend)

    renamed = ScimUserAttribute(
        name="fullName", type="string", custom_attribute_name="displayName"
    )
    ScimRemoteProviderProvider().update(
        created.id, olds, _props(user_attributes=[renamed])
    )
    writes = _attribute_writes(backend)
    assert [method for method, _, _ in writes] == ["PUT"]
    assert writes[0][2]["customAttributeName"] == "displayName"
    assert _default_loads(backend) == 0


@pytest.mark.parametrize(
    ("declared", "kept"),
    [([], set()), ([FULL_NAME], {"fullName"})],
)
def test_undeclared_custom_attributes_are_deleted(
    backend: FakeBackend, declared: list[ScimUserAttribute], kept: set[str]
) -> None:
    """The declared list is the whole set of custom mappings; RFC ones stay."""
    existing = _existing_provider(backend)
    schema_id = backend.schemas[0]["id"]
    for name, kind in (("fullName", "string"), ("emailOptIn", "integer")):
        backend.attributes[f"a-{name}"] = {
            "id": f"a-{name}",
            "schemaId": schema_id,
            "name": name,
            "type": kind,
            "customAttributeName": name,
        }

    ScimRemoteProviderProvider().update(
        existing["id"], {}, _props(user_attributes=declared)
    )

    names = {a["name"] for a in backend.attributes.values()}
    assert names == {"title", *kept}
    assert _default_loads(backend) == 0


def test_undeclared_provider_keeps_its_custom_attributes(
    backend: FakeBackend,
) -> None:
    """None leaves the schema alone, custom mappings included."""
    existing = _existing_provider(backend)
    backend.attributes["a-fullName"] = {
        "id": "a-fullName",
        "schemaId": backend.schemas[0]["id"],
        "name": "fullName",
        "type": "string",
        "customAttributeName": "fullName",
    }

    ScimRemoteProviderProvider().update(
        existing["id"], {}, _props(user_attributes=None)
    )

    assert "a-fullName" in backend.attributes
    assert not _attribute_writes(backend)


def test_live_command_keys_are_never_echoed_into_a_write(
    backend: FakeBackend,
) -> None:
    existing = _existing_provider(backend)
    backend.providers[existing["id"]]["useDefaultScimConfiguration"] = True

    ScimRemoteProviderProvider().update(
        existing["id"], {}, _props(user_attributes=None)
    )

    assert _default_loads(backend) == 0


def test_diff_sees_a_user_attribute_change() -> None:
    declared = _props(user_attributes=[FULL_NAME])
    undeclared = _props(user_attributes=None)

    assert ScimRemoteProviderProvider().diff("p0", declared, undeclared).changes
    assert not ScimRemoteProviderProvider().diff("p0", declared, declared).changes
    # Deployed state predates the input and the engine drops the None value.
    assert "user_attributes" not in undeclared
    assert not ScimRemoteProviderProvider().diff("p0", undeclared, undeclared).changes
