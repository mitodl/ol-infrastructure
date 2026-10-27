"""Pulumi dynamic provider for OpenMetadata bots and their JWTs.

A bot is two entities: a user with ``isBot`` that holds the roles and the
token, and a bot that points at it. ``PUT /v1/users`` creates or updates the
user and, for a new one, generates its token; on an existing bot user it keeps
the token and merges the roles it already holds with the request's, so roles
are set afterwards with a PATCH instead. OpenMetadata gives every bot
``DefaultBotRole`` itself, so it is never declared here.

The token is an output, read back with ``GET /v1/users/auth-mechanism/{id}``
on every refresh, so a token regenerated in the UI reaches its consumers on the
next ``pulumi up --refresh``. Changing ``rotation`` or ``token_expiry``
generates a new one, which revokes the old.

A bot that already exists is adopted rather than failing, as the bots created
by hand under Settings > Bots before this provider were.

``read`` and ``delete`` connect with the credentials saved at the last create or
update, as every dynamic provider does. After the provisioner's client secret
changes, the refresh before an update fails with a 401 until one
``pulumi up --skip-refresh`` saves the new secret.
"""

from typing import Any, Literal

import pulumi
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict, field_validator

from .client import OpenMetadataClient, OpenMetadataConnection

DEFAULT_BOT_ROLE = "DefaultBotRole"

JWTTokenExpiry = Literal["OneHour", "1", "7", "30", "60", "90", "Unlimited"]

_CONNECTION_PROPS = ("server_url", "token_url", "client_id", "client_secret")
_UPDATABLE_PROPS = ("display_name", "description", "roles")
_TOKEN_PROPS = ("token_expiry", "rotation")


class OpenMetadataBotSpec(BaseModel):
    """Declared state of one bot.

    :param name: The bot's name, which is also its user's name.
    :param display_name: Shown in the OpenMetadata UI.
    :param description: Shown on the bot's page.
    :param email: The bot user's email. OpenMetadata requires one, rejects a
        duplicate, and keeps the original on every update, so it is set only
        when the bot is created.
    :param roles: The roles the bot holds besides ``DefaultBotRole``.
    :param token_expiry: How long a generated token lasts, in days.
    :param rotation: Any value; changing it generates a new token. Empty
        rather than None, because Pulumi drops inputs that are None.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str
    description: str
    email: str
    roles: list[str]
    token_expiry: JWTTokenExpiry = "Unlimited"  # noqa: S105
    rotation: str = ""

    @field_validator("roles")
    @classmethod
    def roles_exclude_default(cls, roles: list[str]) -> list[str]:
        if DEFAULT_BOT_ROLE in roles:
            msg = f"{DEFAULT_BOT_ROLE} is given to every bot; do not declare it"
            raise ValueError(msg)
        return sorted(set(roles))


def _bot_user(client: OpenMetadataClient, path: str) -> dict[str, Any] | None:
    return client.get(path, fields="roles")


def _held_roles(user: dict[str, Any]) -> list[str]:
    return sorted(
        role["name"] for role in user["roles"] if role["name"] != DEFAULT_BOT_ROLE
    )


def _token(client: OpenMetadataClient, user_id: str) -> str:
    auth_mechanism = client.get(f"users/auth-mechanism/{user_id}")
    if auth_mechanism is None:
        msg = f"bot user {user_id} has no authentication mechanism"
        raise RuntimeError(msg)
    return auth_mechanism["config"]["JWTToken"]


def _apply(client: OpenMetadataClient, props: dict[str, Any]) -> dict[str, Any]:
    """Create or update the bot user and bot, then put its roles and admin flag
    back to what is declared.

    :returns: The bot user.
    :raises ValueError: If a person already has the bot's name, or a declared
        role does not exist.
    """
    name = props["name"]
    existing = client.get(f"users/name/{name}")
    if existing is not None and existing.get("isBot") is not True:
        msg = f"{name!r} is a user, not a bot"
        raise ValueError(msg)

    client.put(
        "users",
        {
            "name": name,
            "email": props["email"],
            "displayName": props["display_name"],
            "description": props["description"],
            "isBot": True,
            "botName": name,
            # Used only when the user has no JWT yet; an existing token is kept.
            "authenticationMechanism": {
                "authType": "JWT",
                "config": {"JWTTokenExpiry": props["token_expiry"]},
            },
        },
    )
    client.put(
        "bots",
        {
            "name": name,
            "botUser": name,
            "displayName": props["display_name"],
            "description": props["description"],
        },
    )

    user = _bot_user(client, f"users/name/{name}")
    if user is None:
        msg = f"bot user {name!r} is missing after it was created"
        raise RuntimeError(msg)

    # OpenMetadata's authorizer returns before evaluating any policy for an
    # admin, so an admin bot's roles would not limit its token. Demoted on its
    # own first, so a role that fails to resolve below cannot leave it an admin.
    if user.get("isAdmin") is True:
        user = client.patch(
            f"users/{user['id']}",
            [{"op": "replace", "path": "/isAdmin", "value": False}],
        )
    if _held_roles(user) != props["roles"]:
        user = client.patch(
            f"users/{user['id']}",
            [
                {
                    "op": "add",
                    "path": "/roles",
                    "value": client.references(
                        "roles", "role", [DEFAULT_BOT_ROLE, *props["roles"]]
                    ),
                }
            ],
        )
    return user


def _bot_in_sync(client: OpenMetadataClient, name: str, user: dict[str, Any]) -> bool:
    # The bot entity carries its own displayName and description, which can
    # change apart from its user's.
    bot = client.get(f"bots/name/{name}")
    return (
        bot is not None
        and bot.get("displayName") == user.get("displayName")
        and bot.get("description") == user.get("description")
    )


def _outs(
    client: OpenMetadataClient, props: dict[str, Any], user: dict[str, Any]
) -> dict[str, Any]:
    return {
        **props,
        "user_id": user["id"],
        "is_admin": user.get("isAdmin") is True,
        "bot_in_sync": _bot_in_sync(client, props["name"], user),
        "token": _token(client, user["id"]),
    }


class OpenMetadataBotProvider(dynamic.ResourceProvider):
    """CRUD for one OpenMetadata bot."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        client = OpenMetadataClient(props)
        user = _apply(client, props)
        return dynamic.CreateResult(id_=user["id"], outs=_outs(client, props, user))

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        client = OpenMetadataClient(props)
        user = _bot_user(client, f"users/{id_}")
        if user is None:
            # An empty id tells the engine the bot is gone, so one deleted in the
            # UI is created again on the next update.
            return dynamic.ReadResult(id_="", outs={})
        return dynamic.ReadResult(
            id_=id_,
            outs=_outs(
                client,
                {
                    **props,
                    "display_name": user.get("displayName"),
                    "description": user.get("description"),
                    "roles": _held_roles(user),
                },
                user,
            ),
        )

    def diff(
        self, _id: str, olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.DiffResult:
        changed = [
            key
            for key in (*_CONNECTION_PROPS, *_UPDATABLE_PROPS, *_TOKEN_PROPS)
            if olds.get(key) != news[key]
        ]
        if olds.get("is_admin"):
            changed.append("is_admin")
        if olds.get("bot_in_sync") is False:
            changed.append("bot_in_sync")
        replaces = ["name"] if olds["name"] != news["name"] else []
        # The old bot goes first: the replacement may reuse its email, which
        # OpenMetadata keeps unique, and a renamed bot gets a new token anyway.
        return dynamic.DiffResult(
            changes=bool(changed or replaces),
            replaces=replaces,
            delete_before_replace=bool(replaces),
        )

    def update(
        self, id_: str, olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        client = OpenMetadataClient(news)
        # An update cannot change the resource's id, so a server_url that points
        # at a different OpenMetadata would manage a different user than the one
        # in state. A URL alias for the same server still finds it.
        if client.get(f"users/{id_}") is None:
            msg = (
                f"bot user {id_} is not at {news['server_url']}. A bot cannot move "
                "between OpenMetadata servers in place; if it was deleted, refresh."
            )
            raise ValueError(msg)
        user = _apply(client, news)
        if any(olds.get(key) != news[key] for key in _TOKEN_PROPS):
            client.put(
                f"users/generateToken/{id_}", {"JWTTokenExpiry": news["token_expiry"]}
            )
            pulumi.log.info(f"Generated a new token for bot {news['name']}")
        return dynamic.UpdateResult(outs=_outs(client, news, user))

    def delete(self, id_: str, props: dict[str, Any]) -> None:
        client = OpenMetadataClient(props)
        # Deleting the bot is recursive and takes its user with it; the user is
        # deleted as well in case the bot entity was already gone.
        client.delete(f"bots/name/{props['name']}", hardDelete="true")
        client.delete(f"users/{id_}", hardDelete="true")


class OpenMetadataBot(dynamic.Resource):
    """An OpenMetadata bot whose JWT is a secret output.

    :param name: Pulumi resource name.
    :param connection: The OpenMetadata API and an admin client for it.
    :param spec: The bot's declared state.
    :param opts: Resource options.
    """

    user_id: pulumi.Output[str]
    token: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        connection: OpenMetadataConnection,
        spec: OpenMetadataBotSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            OpenMetadataBotProvider(),
            name,
            {
                **connection.to_props(),
                **spec.model_dump(),
                "user_id": None,
                "is_admin": None,
                "bot_in_sync": None,
                "token": None,
            },
            opts=pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(
                    additional_secret_outputs=["token", "client_secret"]
                ),
            ),
        )
