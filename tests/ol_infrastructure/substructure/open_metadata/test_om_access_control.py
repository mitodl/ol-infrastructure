"""The OpenMetadata bootstrap job's policies, roles and bot check.

The fake server follows what OpenMetadata 2.0.5 does where the job depends on
it: a stored policy has its rules sorted by name, each rule's resources and
operations sorted and a ``fullyQualifiedName`` added; a role's ``policies`` come
back as entity references only when ``fields=policies`` is asked for; and a bot
is a user with ``isBot`` that always holds DefaultBotRole.

What matters is that a second run changes nothing, that rules or policies
changed by hand are put back, and that a bot holding more than its declared
roles fails the job while one that is missing or short of a role does not.
"""

import copy
import importlib
import uuid
from types import ModuleType
from typing import Any

import pytest
import requests

SERVER = "http://openmetadata:8585/api"


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeOpenMetadata:
    """Policies, roles and users, keyed by collection then name."""

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, dict[str, Any]]] = {
            "policies": {},
            "roles": {},
            "users": {},
        }
        self.writes: list[tuple[str, str, Any]] = []

    def _by_id(self, collection: str, entity_id: str) -> dict[str, Any]:
        return next(
            e for e in self.entities[collection].values() if e["id"] == entity_id
        )

    def _store_policy(self, policy: dict[str, Any]) -> None:
        rules = []
        for rule in policy["rules"]:
            stored = {
                **rule,
                "fullyQualifiedName": f"{policy['name']}.{rule['name']}",
                "resources": sorted(rule["resources"], key=str.lower),
                "operations": sorted(rule["operations"]),
            }
            rules.append(stored)
        policy["rules"] = sorted(rules, key=lambda rule: rule["name"])

    def add_role(self, name: str, policies: list[str]) -> dict[str, Any]:
        role = {
            "id": str(uuid.uuid4()),
            "name": name,
            "description": "",
            "policies": [
                {
                    "id": self.entities["policies"][p]["id"],
                    "type": "policy",
                    "name": p,
                }
                for p in policies
            ],
        }
        self.entities["roles"][name] = role
        return role

    def add_user(self, name: str, roles: list[str], *, is_bot: bool = True) -> None:
        self.entities["users"][name] = {
            "id": str(uuid.uuid4()),
            "name": name,
            "isBot": is_bot,
            "roles": [{"type": "role", "name": role} for role in roles],
        }

    def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> Any:
        collection, _name, name = url.removeprefix(f"{SERVER}/v1/").split("/", 2)
        entity = self.entities[collection].get(name)
        if entity is None:
            return FakeResponse(404)
        fields = (params or {}).get("fields", "")
        body = copy.deepcopy(entity)
        relation = {"roles": "policies", "users": "roles"}.get(collection)
        if relation and relation not in fields:
            body.pop(relation)
        return FakeResponse(200, body)

    def post(self, url: str, json: dict[str, Any], **_: Any) -> Any:
        collection = url.removeprefix(f"{SERVER}/v1/")
        self.writes.append(("POST", collection, json))
        if collection == "roles":
            entity = self.add_role(json["name"], json["policies"])
            entity["description"] = json["description"]
        else:
            entity = {"id": str(uuid.uuid4()), **copy.deepcopy(json)}
            self._store_policy(entity)
            self.entities[collection][json["name"]] = entity
        return FakeResponse(201, entity)

    def patch(self, url: str, json: list[dict[str, Any]], **_: Any) -> Any:
        collection, entity_id = url.removeprefix(f"{SERVER}/v1/").split("/")
        self.writes.append(("PATCH", f"{collection}/{entity_id}", json))
        entity = self._by_id(collection, entity_id)
        for op in json:
            value = copy.deepcopy(op["value"])
            if op["path"] == "/policies":
                value = [
                    {**ref, "name": self._by_id("policies", ref["id"])["name"]}
                    for ref in value
                ]
            entity[op["path"].lstrip("/")] = value
        if collection == "policies":
            self._store_policy(entity)
        return FakeResponse(200, entity)


@pytest.fixture
def server() -> FakeOpenMetadata:
    return FakeOpenMetadata()


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, server: FakeOpenMetadata) -> ModuleType:
    monkeypatch.setenv("OM_SERVER_URL", SERVER)
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "token")  # pragma: allowlist secret
    module = importlib.import_module(
        "ol_infrastructure.substructure.open_metadata.scripts.om_server_config"
    )
    module = importlib.reload(module)
    monkeypatch.setattr(module.requests, "get", server.get)
    monkeypatch.setattr(module.requests, "post", server.post)
    monkeypatch.setattr(module.requests, "patch", server.patch)
    return module


def _reconcile_access(config: ModuleType) -> None:
    for name, policy in config.POLICY_CONFIG.items():
        config._reconcile_policy(name, policy)
    for name, role in config.ROLE_CONFIG.items():
        config._reconcile_role(name, role)


def test_creates_policy_and_role_then_changes_nothing(
    config: ModuleType, server: FakeOpenMetadata
) -> None:
    _reconcile_access(config)
    assert [(method, collection) for method, collection, _ in server.writes] == [
        *[("POST", "policies")] * len(config.POLICY_CONFIG),
        *[("POST", "roles")] * len(config.ROLE_CONFIG),
    ]

    server.writes.clear()
    _reconcile_access(config)
    assert server.writes == []


def test_puts_back_a_rule_changed_by_hand(
    config: ModuleType, server: FakeOpenMetadata
) -> None:
    _reconcile_access(config)
    stored = server.entities["policies"]["DagsterBotPolicy"]
    stored["rules"][0]["operations"].append("Delete")
    server.writes.clear()

    _reconcile_access(config)

    assert server.writes == [
        (
            "PATCH",
            f"policies/{stored['id']}",
            [
                {
                    "op": "add",
                    "path": "/rules",
                    "value": config.POLICY_CONFIG["DagsterBotPolicy"]["rules"],
                }
            ],
        )
    ]


def test_puts_back_a_policy_added_to_the_role_by_hand(
    config: ModuleType, server: FakeOpenMetadata
) -> None:
    _reconcile_access(config)
    server.entities["policies"]["IngestionBotPolicy"] = {
        "id": str(uuid.uuid4()),
        "name": "IngestionBotPolicy",
        "rules": [],
    }
    role = server.entities["roles"]["DagsterBotRole"]
    role["policies"].append(
        {
            "id": server.entities["policies"]["IngestionBotPolicy"]["id"],
            "type": "policy",
            "name": "IngestionBotPolicy",
        }
    )

    _reconcile_access(config)

    assert [p["name"] for p in role["policies"]] == ["DagsterBotPolicy"]


def test_missing_bot_is_reported_not_failed(
    config: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    config._check_bot("dagster-bot", {"DagsterBotRole"})
    assert "[todo] bot dagster-bot: does not exist" in capsys.readouterr().out


def test_bot_without_its_role_is_reported_not_failed(
    config: ModuleType, server: FakeOpenMetadata, capsys: pytest.CaptureFixture[str]
) -> None:
    server.add_user("dagster-bot", ["DefaultBotRole"])
    config._check_bot("dagster-bot", {"DagsterBotRole"})
    assert "[todo] bot dagster-bot: lacks ['DagsterBotRole']" in capsys.readouterr().out


def test_bot_with_its_role_passes(
    config: ModuleType, server: FakeOpenMetadata, capsys: pytest.CaptureFixture[str]
) -> None:
    server.add_user("dagster-bot", ["DefaultBotRole", "DagsterBotRole"])
    config._check_bot("dagster-bot", {"DagsterBotRole"})
    assert "[ok]   bot dagster-bot" in capsys.readouterr().out


def test_bot_with_an_extra_role_fails(
    config: ModuleType, server: FakeOpenMetadata
) -> None:
    server.add_user(
        "dagster-bot", ["DefaultBotRole", "DagsterBotRole", "IngestionBotRole"]
    )
    with pytest.raises(ValueError, match="IngestionBotRole"):
        config._check_bot("dagster-bot", {"DagsterBotRole"})


def test_a_person_with_the_bot_name_fails(
    config: ModuleType, server: FakeOpenMetadata
) -> None:
    server.add_user("dagster-bot", ["DagsterBotRole"], is_bot=False)
    with pytest.raises(ValueError, match="not a bot"):
        config._check_bot("dagster-bot", {"DagsterBotRole"})
