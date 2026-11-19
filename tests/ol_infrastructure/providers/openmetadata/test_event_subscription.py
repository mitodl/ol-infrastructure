"""The OpenMetadata alert (event subscription) dynamic provider.

The fake server follows OpenMetadata 2.0.6 where the provider depends on it
(EventSubscriptionResource): ``PUT /v1/events/subscriptions`` creates the alert
or updates the one with the same name, keeping its id, and ``GET`` returns the
``input``, ``destinations`` and ``enabled`` it holds.
"""

import copy
import uuid
from typing import Any

import pytest
import requests

from ol_infrastructure.providers.openmetadata import (
    AlertArgument,
    AlertCondition,
    OpenMetadataEventSubscriptionProvider,
    OpenMetadataEventSubscriptionSpec,
)

from .fakes import FakeResponse

SERVER = "https://om.example/api"
TOKEN_URL = "https://keycloak.example/realms/ol/protocol/openid-connect/token"  # noqa: S105
WEBHOOK = "https://hooks.slack.example/services/T/B/x"


class FakeOpenMetadata:
    """Event subscriptions, keyed by id."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, Any]] = {}

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.subscriptions.values() if s["name"] == name), None)

    def handle(self, method: str, url: str, body: Any = None) -> FakeResponse:
        path = url.removeprefix(f"{SERVER}/v1/")
        if method == "PUT" and path == "events/subscriptions":
            existing = self.by_name(body["name"])
            subscription = {
                **copy.deepcopy(body),
                "id": existing["id"] if existing else str(uuid.uuid4()),
                "filteringRules": {"resources": list(body["resources"])},
            }
            self.subscriptions[subscription["id"]] = subscription
            return FakeResponse(method, url, 200, subscription)
        prefix = "events/subscriptions/"
        if path.startswith(prefix):
            subscription_id = path.removeprefix(prefix)
            if method == "GET":
                found = self.subscriptions.get(subscription_id)
                return FakeResponse(method, url, 200 if found else 404, found)
            if method == "DELETE":
                found = self.subscriptions.pop(subscription_id, None)
                return FakeResponse(method, url, 200 if found else 404, found)
        msg = f"unexpected {method} {path}"
        raise AssertionError(msg)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeOpenMetadata:
    fake = FakeOpenMetadata()

    def token(url: str, **_: Any) -> FakeResponse:
        assert url == TOKEN_URL
        return FakeResponse("POST", url, 200, {"access_token": "kc-token"})

    monkeypatch.setattr(requests, "post", token)
    for method in ("get", "put", "delete"):
        monkeypatch.setattr(
            requests.Session,
            method,
            lambda _self, url, *, _method=method.upper(), json=None, **_: fake.handle(
                _method, url, json
            ),
        )
    return fake


def _props(**overrides: Any) -> dict[str, Any]:
    """Build inputs as the engine delivers them, without the None-valued ones."""
    spec = OpenMetadataEventSubscriptionSpec(
        name="data-contract-failures",
        display_name="Data contract failures",
        description="Posts failed contracts.",
        alert_type="Observability",
        resource="dataContract",
        actions=[
            AlertCondition(
                name="GetDataContractStatusUpdates",
                arguments=[
                    AlertArgument(
                        name="contractStatusList", input=["Failed", "Aborted"]
                    )
                ],
            )
        ],
    )
    props = {
        "server_url": SERVER,
        "token_url": TOKEN_URL,
        "client_id": "ol-openmetadata-provisioner",
        "client_secret": "secret",  # pragma: allowlist secret
        **spec.model_dump(),
        "slack_webhook_url": WEBHOOK,
        "in_sync": None,
        **overrides,
    }
    return {key: value for key, value in props.items() if value is not None}


def test_create_puts_the_declared_alert(server: FakeOpenMetadata) -> None:
    result = OpenMetadataEventSubscriptionProvider().create(_props())

    held = server.subscriptions[result.id]
    assert held["alertType"] == "Observability"
    assert held["resources"] == ["dataContract"]
    assert held["enabled"] is True
    assert held["input"] == {
        "filters": [],
        "actions": [
            {
                "name": "GetDataContractStatusUpdates",
                "effect": "include",
                "prefixCondition": "AND",
                "arguments": [
                    {"name": "contractStatusList", "input": ["Failed", "Aborted"]}
                ],
            }
        ],
    }
    assert held["destinations"] == [
        {"category": "External", "type": "Slack", "config": {"endpoint": WEBHOOK}}
    ]
    assert result.outs["in_sync"] is True


def test_create_adopts_an_alert_made_by_hand(server: FakeOpenMetadata) -> None:
    server.subscriptions["hand-made"] = {
        "id": "hand-made",
        "name": "data-contract-failures",
        "enabled": False,
    }

    result = OpenMetadataEventSubscriptionProvider().create(_props())

    assert result.id == "hand-made"
    assert server.subscriptions["hand-made"]["enabled"] is True


def test_read_is_in_sync_when_the_server_reorders_arguments(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())
    arguments = server.subscriptions[created.id]["input"]["actions"][0]["arguments"]
    arguments[0]["input"].reverse()

    assert provider.read(created.id, created.outs).outs["in_sync"] is True


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda s: s.update(enabled=False), id="disabled"),
        pytest.param(
            lambda s: s["destinations"][0]["config"].update(endpoint="https://x"),
            id="webhook",
        ),
        pytest.param(
            lambda s: s["input"]["actions"][0]["arguments"][0].update(input=["Failed"]),
            id="statuses",
        ),
        pytest.param(
            lambda s: s["input"]["filters"].append(
                {"name": "filterByOwner", "arguments": []}
            ),
            id="filter-added",
        ),
        pytest.param(lambda s: s.update(description="edited"), id="description"),
        pytest.param(lambda s: s.update(alertType="Notification"), id="alert-type"),
        pytest.param(
            lambda s: s["filteringRules"].update(resources=["testCase"]),
            id="resource",
        ),
    ],
)
def test_read_reports_an_alert_edited_in_the_ui(
    server: FakeOpenMetadata, edit: Any
) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())
    edit(server.subscriptions[created.id])

    read = provider.read(created.id, created.outs)

    assert read.outs["in_sync"] is False
    assert provider.diff(created.id, read.outs, _props()).changes is True


def test_read_reports_a_deleted_alert_as_gone(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())
    del server.subscriptions[created.id]

    assert provider.read(created.id, created.outs).id == ""


@pytest.mark.usefixtures("server")
def test_diff_is_empty_when_nothing_changed() -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())

    assert provider.diff(created.id, created.outs, _props()).changes is False


@pytest.mark.usefixtures("server")
def test_diff_replaces_a_renamed_alert() -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())

    diff = provider.diff(created.id, created.outs, _props(name="renamed"))

    assert diff.replaces == ["name"]


def test_update_puts_the_new_state_back(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())
    server.subscriptions[created.id]["enabled"] = False

    provider.update(created.id, created.outs, _props(slack_webhook_url="https://y"))

    held = server.subscriptions[created.id]
    assert held["enabled"] is True
    assert held["destinations"][0]["config"]["endpoint"] == "https://y"


def test_update_leaves_another_servers_alert_alone(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())
    # The same name on a server that does not hold the id in state.
    server.subscriptions = {
        "other": {**server.subscriptions.pop(created.id), "id": "other"}
    }

    with pytest.raises(ValueError, match="cannot move between"):
        provider.update(created.id, created.outs, _props(slack_webhook_url="https://y"))

    held = server.subscriptions["other"]["destinations"][0]["config"]["endpoint"]
    assert held == WEBHOOK


def test_delete_removes_the_alert(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataEventSubscriptionProvider()
    created = provider.create(_props())

    provider.delete(created.id, created.outs)
    provider.delete(created.id, created.outs)

    assert server.subscriptions == {}
