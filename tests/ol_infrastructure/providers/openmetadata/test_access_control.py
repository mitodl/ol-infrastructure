"""The OpenMetadata policy, role and team dynamic providers.

The fake server follows OpenMetadata 2.0.6 where the providers depend on it
(PolicyRepository, RoleRepository, TeamRepository): a stored policy has its
rules sorted by name, each rule's resources and operations sorted and a
``fullyQualifiedName`` added; a role's ``policies`` and a team's
``defaultRoles`` come back as entity references only when asked for in
``fields``; CreateRole takes policy names and CreateTeam takes role ids.

What matters is that an entity the Job created is adopted with its id, that a
second run changes nothing, that changes made by hand are put back, and that a
team's members are never sent.
"""

import copy
import uuid
from typing import Any

import pytest
import requests

from ol_infrastructure.providers.openmetadata.access_control import (
    OpenMetadataPolicyProvider,
    OpenMetadataPolicyRule,
    OpenMetadataRoleProvider,
    OpenMetadataTeamProvider,
)
from ol_infrastructure.substructure.open_metadata.access_control import (
    POLICIES,
    ROLES,
    TEAMS,
)

from .fakes import FakeResponse

SERVER = "https://om.example/api"
TOKEN_URL = "https://keycloak.example/realms/ol/protocol/openid-connect/token"  # noqa: S105

_RELATIONS = {"roles": ("policies", "policies"), "teams": ("defaultRoles", "roles")}


class FakeOpenMetadata:
    """Policies, roles and teams, keyed by collection then name."""

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, dict[str, Any]]] = {
            "policies": {},
            "roles": {},
            "teams": {},
        }
        self.writes: list[tuple[str, str, Any]] = []
        self.add("policies", "DataStewardPolicy", rules=[])
        self.add(
            "roles", "DataSteward", policies=[self.ref("policies", "DataStewardPolicy")]
        )

    def add(self, collection: str, name: str, **fields: Any) -> dict[str, Any]:
        entity = {"id": str(uuid.uuid4()), "name": name, **fields}
        if collection == "policies":
            self._store_rules(entity)
        self.entities[collection][name] = entity
        return entity

    def ref(self, collection: str, name: str) -> dict[str, str]:
        entity = self.entities[collection][name]
        return {"id": entity["id"], "type": collection[:-1], "name": name}

    def _by_id(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return next(
            (e for e in self.entities[collection].values() if e["id"] == entity_id),
            None,
        )

    def _name_of(self, entity_id: str) -> str:
        return next(
            e["name"]
            for entities in self.entities.values()
            for e in entities.values()
            if e["id"] == entity_id
        )

    @staticmethod
    def _store_rules(policy: dict[str, Any]) -> None:
        policy["rules"] = sorted(
            (
                {
                    **rule,
                    "fullyQualifiedName": f"{policy['name']}.{rule['name']}",
                    "resources": sorted(rule["resources"], key=str.lower),
                    "operations": sorted(rule["operations"]),
                }
                for rule in policy["rules"]
            ),
            key=lambda rule: rule["name"],
        )

    def _view(
        self, collection: str, entity: dict[str, Any] | None, params: dict[str, str]
    ) -> dict[str, Any] | None:
        if entity is None:
            return None
        body = copy.deepcopy(entity)
        if collection in _RELATIONS:
            field, _ = _RELATIONS[collection]
            if field not in params.get("fields", ""):
                body.pop(field, None)
        return body

    def handle(
        self,
        method: str,
        url: str,
        body: Any = None,
        params: dict[str, str] | None = None,
    ) -> FakeResponse:
        collection, _, rest = url.removeprefix(f"{SERVER}/v1/").partition("/")
        params = params or {}
        if method != "GET":
            self.writes.append((method, url.removeprefix(f"{SERVER}/v1/"), body))
        if method == "GET" and rest.startswith("name/"):
            entity = self.entities[collection].get(rest.removeprefix("name/"))
            payload = self._view(collection, entity, params)
        elif method == "GET":
            payload = self._view(collection, self._by_id(collection, rest), params)
        elif method == "POST":
            payload = self._create(collection, body)
        elif method == "PATCH":
            payload = self._patch(collection, rest, body)
        elif method == "DELETE":
            entity = self._by_id(collection, rest)
            payload = None
            if entity is not None:
                del self.entities[collection][entity["name"]]
                payload = {}
        else:
            msg = f"unexpected {method} {url}"
            raise AssertionError(msg)
        return FakeResponse(method, url, 404 if payload is None else 200, payload)

    def _create(self, collection: str, create: dict[str, Any]) -> dict[str, Any]:
        create = copy.deepcopy(create)
        if collection == "roles":
            create["policies"] = [
                self.ref("policies", name) for name in create["policies"]
            ]
        if collection == "teams":
            assert "users" not in create
            create["defaultRoles"] = [
                self.ref("roles", self._name_of(role_id))
                for role_id in create.get("defaultRoles") or []
            ]
            create["users"] = []
        return self.add(collection, create.pop("name"), **create)

    def _patch(
        self, collection: str, entity_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        entity = self._by_id(collection, entity_id)
        assert entity is not None
        for operation in operations:
            assert operation["path"] != "/users"
            value = copy.deepcopy(operation["value"])
            if operation["path"] in {"/policies", "/defaultRoles"}:
                value = [{**ref, "name": self._name_of(ref["id"])} for ref in value]
            entity[operation["path"].lstrip("/")] = value
        if collection == "policies":
            self._store_rules(entity)
        return copy.deepcopy(entity)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeOpenMetadata:
    fake = FakeOpenMetadata()

    def token(url: str, **_: Any) -> FakeResponse:
        assert url == TOKEN_URL
        return FakeResponse("POST", url, 200, {"access_token": "kc-token"})

    monkeypatch.setattr(requests, "post", token)
    for method in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(
            requests.Session,
            method,
            lambda _self, url, *, _method=method.upper(), json=None, params=None, **_: (
                fake.handle(_method, url, json, params)
            ),
        )
    return fake


_CONNECTION = {
    "server_url": SERVER,
    "token_url": TOKEN_URL,
    "client_id": "ol-openmetadata-provisioner",
    "client_secret": "secret",  # pragma: allowlist secret
}


def _props(spec: Any, **overrides: Any) -> dict[str, Any]:
    """Build inputs as the engine delivers them, without the None-valued ones."""
    return {**_CONNECTION, **spec.model_dump(exclude_none=True), **overrides}


def _declared(specs: list[Any], entity: str, **overrides: Any) -> dict[str, Any]:
    return _props(next(spec for spec in specs if spec.name == entity), **overrides)


def _policy(entity: str = "DagsterBotPolicy", **overrides: Any) -> dict[str, Any]:
    return _declared(POLICIES, entity, **overrides)


def _role(entity: str = "DagsterBotRole", **overrides: Any) -> dict[str, Any]:
    return _declared(ROLES, entity, **overrides)


def _team(entity: str = "data-platform", **overrides: Any) -> dict[str, Any]:
    return _declared(TEAMS, entity, **overrides)


def _create_all() -> dict[str, Any]:
    """Create every declared entity, policies before roles before teams."""
    created = {}
    for provider, specs in (
        (OpenMetadataPolicyProvider(), POLICIES),
        (OpenMetadataRoleProvider(), ROLES),
        (OpenMetadataTeamProvider(), TEAMS),
    ):
        for spec in specs:
            created[spec.name] = (provider, provider.create(_props(spec)))
    return created


def test_creates_everything_then_a_refresh_finds_no_drift(
    server: FakeOpenMetadata,
) -> None:
    created = _create_all()

    assert {method for method, _, _ in server.writes} == {"POST"}
    team = server.entities["teams"]["data-platform"]
    assert team["isJoinable"] is False
    assert team["teamType"] == "Group"
    assert [ref["name"] for ref in team["defaultRoles"]] == ["DataSteward"]
    for provider, result in created.values():
        read = provider.read(result.id, result.outs)
        assert read.outs["drift"] == []
        assert not provider.diff(result.id, read.outs, result.outs).changes


def test_adopts_what_the_job_created_without_recreating_it(
    server: FakeOpenMetadata,
) -> None:
    _create_all()
    ids = {
        (collection, name): entity["id"]
        for collection, entities in server.entities.items()
        for name, entity in entities.items()
    }
    server.writes.clear()

    _create_all()

    assert server.writes == []
    assert ids == {
        (collection, name): entity["id"]
        for collection, entities in server.entities.items()
        for name, entity in entities.items()
    }


def test_adopting_puts_back_what_differs(server: FakeOpenMetadata) -> None:
    # As the Job left them: displayName was create-only, so a team made by hand
    # may carry another.
    policy = server.add(
        "policies",
        "DagsterBotPolicy",
        displayName="Dagster Bot Policy",
        description="old",
        rules=[
            {
                "name": "DagsterBotRule-TableTests",
                "effect": "allow",
                "resources": ["table"],
                "operations": ["ViewTests"],
            }
        ],
    )

    result = OpenMetadataPolicyProvider().create(_policy())

    assert result.id == policy["id"]
    [(method, path, operations)] = server.writes
    assert (method, path) == ("PATCH", f"policies/{policy['id']}")
    assert sorted(op["path"] for op in operations) == ["/description", "/rules"]


def test_a_rule_changed_by_hand_is_reported_and_put_back(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataPolicyProvider()
    created = provider.create(_policy())
    server.entities["policies"]["DagsterBotPolicy"]["rules"][0]["operations"].append(
        "Delete"
    )

    read = provider.read(created.id, created.outs)
    assert read.outs["drift"] == ["rules"]
    assert provider.diff(created.id, read.outs, _policy()).changes

    server.writes.clear()
    provider.update(created.id, read.outs, _policy())
    assert server.writes == [
        (
            "PATCH",
            f"policies/{created.id}",
            [{"op": "add", "path": "/rules", "value": _policy()["rules"]}],
        )
    ]
    assert provider.read(created.id, created.outs).outs["drift"] == []


def test_a_policy_added_to_a_role_by_hand_is_put_back(
    server: FakeOpenMetadata,
) -> None:
    OpenMetadataPolicyProvider().create(_policy())
    provider = OpenMetadataRoleProvider()
    created = provider.create(_role())
    role = server.entities["roles"]["DagsterBotRole"]
    role["policies"].append(server.ref("policies", "DataStewardPolicy"))

    read = provider.read(created.id, created.outs)
    assert read.outs["drift"] == ["policies"]

    provider.update(created.id, read.outs, _role())
    assert [ref["name"] for ref in role["policies"]] == ["DagsterBotPolicy"]


def test_a_team_is_adopted_with_its_members_and_unmanaged_default_roles(
    server: FakeOpenMetadata,
) -> None:
    members = [{"id": str(uuid.uuid4()), "type": "user", "name": "someone"}]
    hand_set = [server.ref("roles", "DataSteward")]
    team = server.add(
        "teams",
        "analytics",
        teamType="Group",
        displayName="Analysts",
        description="Analysts who work with the warehouse.",
        users=members,
        defaultRoles=hand_set,
    )

    result = OpenMetadataTeamProvider().create(_team("analytics"))

    assert result.id == team["id"]
    assert server.writes == [
        (
            "PATCH",
            f"teams/{team['id']}",
            [{"op": "add", "path": "/displayName", "value": "Analytics"}],
        )
    ]
    assert team["users"] == members
    assert team["defaultRoles"] == hand_set


def test_declared_default_roles_are_exact(server: FakeOpenMetadata) -> None:
    OpenMetadataPolicyProvider().create(_policy())
    OpenMetadataRoleProvider().create(_role())
    provider = OpenMetadataTeamProvider()
    created = provider.create(_team())
    team = server.entities["teams"]["data-platform"]
    team["defaultRoles"].append(server.ref("roles", "DagsterBotRole"))

    read = provider.read(created.id, created.outs)
    assert read.outs["drift"] == ["default_roles"]

    provider.update(created.id, read.outs, _team())
    assert [ref["name"] for ref in team["defaultRoles"]] == ["DataSteward"]


def test_a_team_of_another_type_is_refused(server: FakeOpenMetadata) -> None:
    server.add("teams", "data-platform", teamType="Department", defaultRoles=[])

    with pytest.raises(ValueError, match="teamType 'Department'"):
        OpenMetadataTeamProvider().create(_team())
    assert server.writes == []


def test_a_role_naming_a_missing_policy_fails(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataRoleProvider()
    created = provider.create(_role("DataContributor", policies=[]))

    with pytest.raises(ValueError, match="policy 'NoSuchPolicy' does not exist"):
        provider.update(
            created.id,
            created.outs,
            _role("DataContributor", policies=["NoSuchPolicy"]),
        )
    assert server.entities["roles"]["DataContributor"]["policies"] == []


def test_read_of_a_deleted_entity_drops_it(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataPolicyProvider()
    created = provider.create(_policy())
    del server.entities["policies"]["DagsterBotPolicy"]

    assert provider.read(created.id, created.outs).id == ""


def test_update_refuses_an_entity_not_at_the_server(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataPolicyProvider()
    created = provider.create(_policy())

    with pytest.raises(ValueError, match="cannot move between OpenMetadata"):
        provider.update(str(uuid.uuid4()), created.outs, _policy(description="new"))
    assert server.entities["policies"]["DagsterBotPolicy"]["description"] != "new"


@pytest.mark.usefixtures("server")
def test_diff() -> None:
    provider = OpenMetadataRoleProvider()
    OpenMetadataPolicyProvider().create(_policy())
    created = provider.create(_role())
    outs = created.outs

    assert not provider.diff(created.id, outs, _role()).changes
    assert provider.diff(created.id, outs, _role(description="new")).changes
    rotated = _role(client_secret="rotated")  # pragma: allowlist secret
    assert provider.diff(created.id, outs, rotated).changes
    assert provider.diff(created.id, {**outs, "drift": ["policies"]}, _role()).changes
    assert provider.diff(created.id, outs, _role(name="Other")).replaces == ["name"]


def test_delete_hard_deletes_and_tolerates_a_missing_entity(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataPolicyProvider()
    created = provider.create(_policy())

    provider.delete(created.id, created.outs)
    provider.delete(created.id, created.outs)

    assert "DagsterBotPolicy" not in server.entities["policies"]
    assert ("DELETE", f"policies/{created.id}", None) in server.writes


@pytest.mark.parametrize(
    ("resources", "operations", "message"),
    [
        (["table"], ["EditAll", "EditTags"], "EditAll covers every Edit"),
        (["table"], ["ViewAll", "ViewBasic"], "ViewAll covers every View"),
        (["All", "table"], ["ViewAll"], "All covers every resource"),
    ],
)
def test_rule_rejects_what_the_server_would_drop(
    resources: list[str], operations: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        OpenMetadataPolicyRule(
            name="r",
            description="",
            effect="allow",
            resources=resources,
            operations=operations,
        )
