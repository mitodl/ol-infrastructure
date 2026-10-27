"""The OpenMetadata bot dynamic provider.

The fake server follows OpenMetadata 2.0.5 where the provider depends on it
(UserResource, BotResource): ``PUT /v1/users`` for a new bot user generates its
token and gives it DefaultBotRole; for an existing one it keeps the token and
the roles it holds; a user's ``roles`` come back only when ``fields=roles`` is
asked for; and deleting a bot deletes its user.
"""

import copy
import uuid
from typing import Any

import pytest
import requests

from ol_infrastructure.providers.openmetadata import (
    OpenMetadataBotProvider,
    OpenMetadataBotSpec,
)

from .fakes import FakeResponse

SERVER = "https://om.example/api"
TOKEN_URL = "https://keycloak.example/realms/ol/protocol/openid-connect/token"  # noqa: S105


class FakeOpenMetadata:
    """Roles, bot users and bots, keyed by name."""

    def __init__(self) -> None:
        self.roles: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.bots: dict[str, dict[str, Any]] = {}
        self.tokens_issued = 0
        for role in ("DefaultBotRole", "DagsterBotRole", "IngestionBotRole"):
            self.roles[role] = {"id": str(uuid.uuid4()), "name": role}

    def _new_token(self) -> str:
        self.tokens_issued += 1
        return f"jwt-{self.tokens_issued}"

    def _role_ref(self, name: str) -> dict[str, str]:
        return {"id": self.roles[name]["id"], "type": "role", "name": name}

    def _user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["id"] == user_id), None)

    def add_user(
        self,
        name: str,
        roles: list[str],
        *,
        is_bot: bool = True,
        is_admin: bool = False,
        token: str = "hand-made",  # noqa: S107
    ) -> dict[str, Any]:
        user = {
            "id": str(uuid.uuid4()),
            "name": name,
            "email": f"{name}@mit.edu",
            "displayName": name,
            "description": "",
            "isBot": is_bot,
            "isAdmin": is_admin,
            "roles": [self._role_ref(role) for role in roles],
            "token": token,
        }
        self.users[name] = user
        return user

    def _view(
        self, user: dict[str, Any] | None, params: dict[str, str]
    ) -> dict[str, Any] | None:
        if user is None:
            return None
        body = {k: v for k, v in user.items() if k != "token"}
        if "roles" not in params.get("fields", ""):
            body.pop("roles")
        return body

    def handle(
        self,
        method: str,
        url: str,
        body: Any = None,
        params: dict[str, str] | None = None,
    ) -> FakeResponse:
        """Route a request; a handler that returns None means 404."""
        path = url.removeprefix(f"{SERVER}/v1/")
        params = params or {}
        # Checked in order, so a longer prefix comes before a shorter one.
        routes: dict[tuple[str, str], Any] = {
            ("GET", "roles/name/"): self.roles.get,
            ("GET", "bots/name/"): self.bots.get,
            ("GET", "users/auth-mechanism/"): self._auth_mechanism,
            ("GET", "users/name/"): lambda key: self._view(self.users.get(key), params),
            ("GET", "users/"): lambda key: self._view(self._user_by_id(key), params),
            ("PUT", "users/generateToken/"): self._generate_token,
            ("PUT", "users"): lambda _key: self._put_user(body),
            ("PUT", "bots"): lambda _key: self._put_bot(body),
            ("PATCH", "users/"): lambda key: self._patch_user(key, body),
            ("DELETE", "bots/name/"): self._delete_bot,
            ("DELETE", "users/"): self._delete_user,
        }
        for (route_method, prefix), handler in routes.items():
            if method == route_method and path.startswith(prefix):
                payload = handler(path.removeprefix(prefix))
                status = 404 if payload is None else 200
                return FakeResponse(method, url, status, payload)
        msg = f"unexpected {method} {path}"
        raise AssertionError(msg)

    def _auth_mechanism(self, user_id: str) -> dict[str, Any] | None:
        user = self._user_by_id(user_id)
        if user is None:
            return None
        return {"authType": "JWT", "config": {"JWTToken": user["token"]}}

    def _generate_token(self, user_id: str) -> dict[str, Any]:
        user = self._user_by_id(user_id)
        assert user is not None
        user["token"] = self._new_token()
        return {"JWTToken": user["token"]}

    def _put_bot(self, create: dict[str, Any]) -> dict[str, Any]:
        self.bots[create["name"]] = copy.deepcopy(create)
        return create

    def _patch_user(
        self, user_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        user = self._user_by_id(user_id)
        assert user is not None
        names_by_id = {role["id"]: name for name, role in self.roles.items()}
        for operation in operations:
            if operation["path"] == "/roles":
                user["roles"] = [
                    self._role_ref(names_by_id[ref["id"]]) for ref in operation["value"]
                ]
            elif operation["path"] == "/isAdmin":
                user["isAdmin"] = operation["value"]
        return self._view(user, {"fields": "roles"})

    def _delete_bot(self, name: str) -> dict[str, Any] | None:
        bot = self.bots.pop(name, None)
        if bot is None:
            return None
        self.users.pop(bot["botUser"], None)
        return {}

    def _delete_user(self, user_id: str) -> dict[str, Any] | None:
        user = self._user_by_id(user_id)
        if user is None:
            return None
        del self.users[user["name"]]
        return {}

    def _put_user(self, create: dict[str, Any]) -> dict[str, Any] | None:
        assert create["isBot"] is True
        existing = self.users.get(create["name"])
        if existing is None:
            user = self.add_user(
                create["name"],
                ["DefaultBotRole"],
                token=self._new_token(),
            )
        else:
            user = existing
        user.update(
            email=create["email"],
            displayName=create["displayName"],
            description=create["description"],
        )
        return self._view(user, {"fields": "roles"})


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeOpenMetadata:
    fake = FakeOpenMetadata()

    def token(url: str, **_: Any) -> FakeResponse:
        assert url == TOKEN_URL
        return FakeResponse("POST", url, 200, {"access_token": "kc-token"})

    monkeypatch.setattr(requests, "post", token)
    for method in ("get", "put", "patch", "delete"):
        monkeypatch.setattr(
            requests.Session,
            method,
            lambda _self, url, *, _method=method.upper(), json=None, params=None, **_: (
                fake.handle(_method, url, json, params)
            ),
        )
    return fake


def _props(**overrides: Any) -> dict[str, Any]:
    """Build inputs as the engine delivers them, without the None-valued ones."""
    spec = OpenMetadataBotSpec(
        name="dagster-bot",
        display_name="Dagster",
        description="Writes Dagster asset check results.",
        email="dagster-bot@mit.edu",
        roles=["DagsterBotRole"],
    )
    props = {
        "server_url": SERVER,
        "token_url": TOKEN_URL,
        "client_id": "ol-openmetadata-provisioner",
        "client_secret": "secret",  # pragma: allowlist secret
        **spec.model_dump(),
        "user_id": None,
        "is_admin": None,
        "token": None,
        **overrides,
    }
    return {key: value for key, value in props.items() if value is not None}


def _role_names(user: dict[str, Any]) -> list[str]:
    return sorted(role["name"] for role in user["roles"])


def test_spec_rejects_default_bot_role() -> None:
    with pytest.raises(ValueError, match="DefaultBotRole is given to every bot"):
        OpenMetadataBotSpec(
            name="b",
            display_name="b",
            description="",
            email="b@mit.edu",
            roles=["DefaultBotRole"],
        )


def test_spec_sorts_and_dedupes_roles() -> None:
    spec = OpenMetadataBotSpec(
        name="b",
        display_name="b",
        description="",
        email="b@mit.edu",
        roles=["Z", "A", "Z"],
    )
    assert spec.roles == ["A", "Z"]


def test_create_makes_the_bot_with_its_roles_and_returns_its_token(
    server: FakeOpenMetadata,
) -> None:
    result = OpenMetadataBotProvider().create(_props())

    user = server.users["dagster-bot"]
    assert result.id == user["id"]
    assert _role_names(user) == ["DagsterBotRole", "DefaultBotRole"]
    assert server.bots["dagster-bot"]["botUser"] == "dagster-bot"
    assert result.outs["token"] == "jwt-1"  # noqa: S105
    assert result.outs["is_admin"] is False


def test_create_adopts_a_hand_made_bot_and_keeps_its_token(
    server: FakeOpenMetadata,
) -> None:
    server.add_user(
        "dagster-bot",
        ["DefaultBotRole", "DagsterBotRole", "IngestionBotRole"],
        is_admin=True,
    )

    result = OpenMetadataBotProvider().create(_props())

    user = server.users["dagster-bot"]
    assert _role_names(user) == ["DagsterBotRole", "DefaultBotRole"]
    assert user["isAdmin"] is False
    assert result.outs["token"] == "hand-made"  # noqa: S105
    assert server.tokens_issued == 0


def test_create_demotes_an_admin_bot_before_a_role_fails(
    server: FakeOpenMetadata,
) -> None:
    server.add_user("dagster-bot", ["DefaultBotRole"], is_admin=True)

    with pytest.raises(ValueError, match="role 'NoSuchRole' does not exist"):
        OpenMetadataBotProvider().create(_props(roles=["NoSuchRole"]))

    assert server.users["dagster-bot"]["isAdmin"] is False


def test_create_refuses_a_person_with_the_bot_name(server: FakeOpenMetadata) -> None:
    server.add_user("dagster-bot", [], is_bot=False)
    with pytest.raises(ValueError, match="is a user, not a bot"):
        OpenMetadataBotProvider().create(_props())


@pytest.mark.usefixtures("server")
def test_create_fails_on_a_missing_role() -> None:
    with pytest.raises(ValueError, match="role 'NoSuchRole' does not exist"):
        OpenMetadataBotProvider().create(_props(roles=["NoSuchRole"]))


def test_read_reports_roles_and_token_changed_in_the_ui(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())
    user = server.users["dagster-bot"]
    user["roles"].append(server._role_ref("IngestionBotRole"))
    user["token"] = "regenerated-in-ui"  # noqa: S105

    read = provider.read(created.id, created.outs)

    assert read.outs["roles"] == ["DagsterBotRole", "IngestionBotRole"]
    assert read.outs["token"] == "regenerated-in-ui"  # noqa: S105
    assert provider.diff(created.id, read.outs, _props()).changes


def test_bot_metadata_changed_apart_from_its_user_is_put_back(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())
    server.bots["dagster-bot"]["description"] = "edited in the UI"

    read = provider.read(created.id, created.outs)
    assert read.outs["bot_in_sync"] is False
    assert provider.diff(created.id, read.outs, _props()).changes

    updated = provider.update(created.id, read.outs, _props())
    assert server.bots["dagster-bot"]["description"] == _props()["description"]
    assert updated.outs["bot_in_sync"] is True


def test_update_refuses_a_bot_not_at_the_server(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())

    with pytest.raises(ValueError, match="cannot move between OpenMetadata"):
        provider.update(str(uuid.uuid4()), created.outs, _props(description="new"))
    assert server.users["dagster-bot"]["description"] != "new"


def test_read_of_a_deleted_bot_drops_it(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())
    del server.users["dagster-bot"]

    assert provider.read(created.id, created.outs).id == ""


@pytest.mark.usefixtures("server")
def test_diff() -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())
    outs = created.outs

    assert not provider.diff(created.id, outs, _props()).changes
    assert provider.diff(created.id, outs, _props(description="new")).changes
    assert provider.diff(created.id, outs, _props(rotation="2026-10")).changes
    # OpenMetadata keeps a user's original email, so it is not diffed.
    assert not provider.diff(created.id, outs, _props(email="x@mit.edu")).changes
    renamed = provider.diff(created.id, outs, _props(name="other-bot"))
    assert renamed.replaces == ["name"]
    assert renamed.delete_before_replace
    assert provider.diff(created.id, {**outs, "is_admin": True}, _props()).changes


def test_update_regenerates_the_token_only_when_rotation_changes(
    server: FakeOpenMetadata,
) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())

    described = provider.update(created.id, created.outs, _props(description="new"))
    assert described.outs["token"] == "jwt-1"  # noqa: S105
    assert server.users["dagster-bot"]["description"] == "new"

    rotated = provider.update(created.id, described.outs, _props(rotation="2026-10"))
    assert rotated.outs["token"] == "jwt-2"  # noqa: S105


def test_delete_removes_the_bot_and_its_user(server: FakeOpenMetadata) -> None:
    provider = OpenMetadataBotProvider()
    created = provider.create(_props())

    provider.delete(created.id, created.outs)
    provider.delete(created.id, created.outs)

    assert server.users == {}
    assert server.bots == {}
