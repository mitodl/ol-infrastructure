"""The OpenMetadata bootstrap job's custom properties.

The fake server follows OpenMetadata 2.0.5: ``GET /v1/metadata/types/name/<type>``
returns ``customProperties`` (each with ``propertyType`` as a reference that
carries the type's name) only when ``fields=customProperties`` is asked for, and
``PUT /v1/metadata/types/<id>`` takes one CustomProperty whose ``propertyType``
references the property type by id.

What matters is that a missing property is added with that body, that a second
run writes nothing, and that a property stored with another type fails the job
instead of being overwritten.
"""

import copy
import importlib
import uuid
from types import ModuleType
from typing import Any

import pytest
import requests

SERVER = "http://openmetadata:8585/api"
TYPES = f"{SERVER}/v1/metadata/types"


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeTypes:
    """Entity and property types keyed by name, with the PUTs sent to them."""

    def __init__(self) -> None:
        self.types: dict[str, dict[str, Any]] = {
            name: {"id": str(uuid.uuid4()), "name": name, "customProperties": []}
            for name in ("metric", "string", "markdown")
        }
        self.puts: list[tuple[str, dict[str, Any]]] = []

    def add_property(self, entity_type: str, prop: dict[str, Any]) -> None:
        stored = {
            **prop,
            "propertyType": {"type": "type", "name": prop["propertyType"]},
        }
        self.types[entity_type]["customProperties"].append(stored)

    def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> Any:
        name = url.removeprefix(f"{TYPES}/name/")
        entity = self.types.get(name)
        if entity is None:
            return FakeResponse(404)
        body = copy.deepcopy(entity)
        if "customProperties" not in (params or {}).get("fields", ""):
            body.pop("customProperties")
        return FakeResponse(200, body)

    def put(self, url: str, json: dict[str, Any], **_: Any) -> Any:
        type_id = url.removeprefix(f"{TYPES}/")
        self.puts.append((type_id, json))
        return FakeResponse(200, {})


@pytest.fixture
def server() -> FakeTypes:
    return FakeTypes()


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, server: FakeTypes) -> ModuleType:
    monkeypatch.setenv("OM_SERVER_URL", SERVER)
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "token")  # pragma: allowlist secret
    module = importlib.import_module(
        "ol_infrastructure.substructure.open_metadata.scripts.om_server_config"
    )
    module = importlib.reload(module)
    monkeypatch.setattr(module.requests, "get", server.get)
    monkeypatch.setattr(module.requests, "put", server.put)
    return module


def _marker(config: ModuleType) -> dict[str, str]:
    return config.CUSTOM_PROPERTY_CONFIG["metric"]["metricFileSha256"]


def test_missing_property_is_added_referencing_the_string_type(
    config: ModuleType, server: FakeTypes
) -> None:
    config._reconcile_custom_property("metric", "metricFileSha256", _marker(config))

    assert server.puts == [
        (
            server.types["metric"]["id"],
            {
                "name": "metricFileSha256",
                "displayName": "Metric file SHA-256",
                "description": _marker(config)["description"],
                "propertyType": {"id": server.types["string"]["id"], "type": "type"},
            },
        )
    ]


def test_property_at_desired_state_is_not_written(
    config: ModuleType, server: FakeTypes, capsys: pytest.CaptureFixture[str]
) -> None:
    server.add_property("metric", {"name": "metricFileSha256", **_marker(config)})

    config._reconcile_custom_property("metric", "metricFileSha256", _marker(config))

    assert server.puts == []
    assert "[ok]   metric custom property metricFileSha256" in capsys.readouterr().out


def test_changed_description_is_put_back(
    config: ModuleType, server: FakeTypes, capsys: pytest.CaptureFixture[str]
) -> None:
    server.add_property(
        "metric",
        {"name": "metricFileSha256", **_marker(config), "description": "edited"},
    )

    config._reconcile_custom_property("metric", "metricFileSha256", _marker(config))

    assert [body["description"] for _, body in server.puts] == [
        _marker(config)["description"]
    ]
    assert "updated description" in capsys.readouterr().out


def test_property_with_another_type_fails(
    config: ModuleType, server: FakeTypes
) -> None:
    server.add_property(
        "metric",
        {"name": "metricFileSha256", **_marker(config), "propertyType": "markdown"},
    )

    with pytest.raises(ValueError, match="propertyType 'markdown'"):
        config._reconcile_custom_property("metric", "metricFileSha256", _marker(config))
    assert server.puts == []
