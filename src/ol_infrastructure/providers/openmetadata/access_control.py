"""Pulumi dynamic providers for OpenMetadata policies, roles and teams.

Code owns what these declare: ``read`` reports each field the server no longer
holds as declared (``drift``), and the next update PATCHes it back.  A team's
members are the exception: they are assigned in the OpenMetadata UI, so a team
is never sent ``users``.

One that already exists is adopted rather than failing, because the
om_server_config Job created them before these providers did: ``create`` finds
it by name, PATCHes what differs and keeps its id, so nothing that refers to it
(users holding a role, a team's assets) is lost.

These connect with the same admin client as the bots (see client.py), so they
can change display names, which the ingestion bot's policy denies.
"""

from typing import Any, ClassVar, Literal

import pulumi
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .client import OpenMetadataClient, OpenMetadataConnection

_CONNECTION_PROPS = ("server_url", "token_url", "client_id", "client_secret")
_TEAM_TYPE = "Group"


def _sorted_names(names: list[str]) -> list[str]:
    return sorted(set(names))


class OpenMetadataPolicyRule(BaseModel):
    """One rule of a policy, with the API's field names.

    :param name: Unique within the policy.
    :param description: Shown on the policy's page.
    :param effect: Whether a match allows or denies the operation.
    :param resources: Entity types, e.g. ``table``, or ``All``.
    :param operations: Operations, e.g. ``EditTags``.
    :param condition: A SpEL expression that must also hold, e.g.
        ``isOwner()``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    effect: Literal["allow", "deny"]
    resources: list[str]
    operations: list[str]
    condition: str | None = None

    @model_validator(mode="after")
    def no_redundant_entries(self) -> "OpenMetadataPolicyRule":
        # The server drops what EditAll, ViewAll or All already cover
        # (PolicyRepository.validateRules), so a rule declaring both would
        # never match what the server stores and would be patched on every run.
        for everything, prefix in (("EditAll", "Edit"), ("ViewAll", "View")):
            if everything in self.operations and any(
                operation != everything and operation.startswith(prefix)
                for operation in self.operations
            ):
                msg = f"{everything} covers every {prefix} operation; drop the others"
                raise ValueError(msg)
        if len(self.resources) > 1 and any(
            resource.lower() == "all" for resource in self.resources
        ):
            msg = "All covers every resource; drop the others"
            raise ValueError(msg)
        return self


class OpenMetadataPolicySpec(BaseModel):
    """Declared state of one policy.

    :param name: The policy's name, which roles refer to.
    :param display_name: Shown in the OpenMetadata UI.
    :param description: Shown on the policy's page.
    :param rules: The policy's rules.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str
    description: str
    rules: list[OpenMetadataPolicyRule]


class OpenMetadataRoleSpec(BaseModel):
    """Declared state of one role.

    :param name: The role's name, which users, teams and Keycloak's ``roles``
        claim refer to.
    :param display_name: Shown in the OpenMetadata UI.
    :param description: Shown on the role's page.
    :param policies: The names of the policies the role grants.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str
    description: str
    policies: list[str]

    _sort_policies = field_validator("policies")(_sorted_names)


class OpenMetadataTeamSpec(BaseModel):
    """Declared state of one ``Group`` team under ``Organization``.

    :param name: The team's name, which asset ownership refers to.
    :param display_name: Shown in the OpenMetadata UI.
    :param description: Shown on the team's page.
    :param default_roles: The exact roles every member inherits, or None to
        leave the team's default roles as they are.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str
    description: str
    default_roles: list[str] | None = None

    @field_validator("default_roles")
    @classmethod
    def sort_default_roles(cls, roles: list[str] | None) -> list[str] | None:
        return None if roles is None else _sorted_names(roles)


def _ref_names(refs: list[dict[str, Any]] | None) -> list[str]:
    return sorted(ref["name"] for ref in refs or [])


def _normalized_rules(rules: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Reduce rules to a form that ignores the server's reordering.

    The server sorts rules by name and each rule's resources and operations,
    and adds a ``fullyQualifiedName`` to each.
    """
    return sorted(
        (
            rule["name"],
            rule["effect"],
            frozenset(resource.lower() for resource in rule["resources"]),
            frozenset(rule["operations"]),
            rule.get("condition"),
            rule.get("description"),
        )
        for rule in rules
    )


class _AccessControlProvider(dynamic.ResourceProvider):
    """Create-or-adopt, drift and PATCH for one kind of entity.

    :cvar collection: The API collection, e.g. ``roles``.
    :cvar kind: The entity type, for messages.
    :cvar fields: Extra fields to ask for, e.g. ``policies``.
    :cvar managed: Declared properties that ``read`` checks and ``update``
        PATCHes back, with the API field each one sets.
    """

    collection: ClassVar[str]
    kind: ClassVar[str]
    fields: ClassVar[str | None] = None
    managed: ClassVar[dict[str, str]] = {
        "display_name": "displayName",
        "description": "description",
    }

    def _get(self, client: OpenMetadataClient, path: str) -> dict[str, Any] | None:
        if self.fields is None:
            return client.get(path)
        return client.get(path, fields=self.fields)

    def _create_body(
        self, client: OpenMetadataClient, props: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _held(self, entity: dict[str, Any], prop: str) -> Any:
        """Return what the server holds for a managed property, in the form its
        declared value is compared in.
        """
        return entity.get(self.managed[prop])

    def _declared(self, props: dict[str, Any], prop: str) -> Any:
        return props[prop]

    def _value(
        self, _client: OpenMetadataClient, props: dict[str, Any], prop: str
    ) -> Any:
        """Return the value a PATCH sets for a managed property."""
        return props[prop]

    def _check(self, entity: dict[str, Any]) -> None:
        """Refuse an entity these resources cannot manage."""

    def _drift(self, entity: dict[str, Any], props: dict[str, Any]) -> list[str]:
        # A property missing from props is not managed, e.g. a team's
        # default_roles left as None, which Pulumi drops.
        return [
            prop
            for prop in self.managed
            if prop in props and self._held(entity, prop) != self._declared(props, prop)
        ]

    def _reconcile(
        self, client: OpenMetadataClient, entity: dict[str, Any], props: dict[str, Any]
    ) -> None:
        self._check(entity)
        if drift := self._drift(entity, props):
            # "add" sets a field that is absent, which "replace" rejects, e.g.
            # the description of an entity made by hand without one.
            client.patch(
                f"{self.collection}/{entity['id']}",
                [
                    {
                        "op": "add",
                        "path": f"/{self.managed[prop]}",
                        "value": self._value(client, props, prop),
                    }
                    for prop in drift
                ],
            )

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        client = OpenMetadataClient(props)
        entity = self._get(client, f"{self.collection}/name/{props['name']}")
        if entity is None:
            entity = client.post(self.collection, self._create_body(client, props))
        else:
            self._reconcile(client, entity, props)
            pulumi.log.info(f"Adopted the existing {self.kind} {props['name']}")
        return dynamic.CreateResult(id_=entity["id"], outs={**props, "drift": []})

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        entity = self._get(OpenMetadataClient(props), f"{self.collection}/{id_}")
        if entity is None:
            # An empty id tells the engine the entity is gone, so one deleted in
            # the UI is created again on the next update.
            return dynamic.ReadResult(id_="", outs={})
        return dynamic.ReadResult(
            id_=id_, outs={**props, "drift": self._drift(entity, props)}
        )

    def diff(
        self, _id: str, olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.DiffResult:
        changed = [
            key
            for key in (*_CONNECTION_PROPS, *self.managed)
            if olds.get(key) != news.get(key)
        ]
        if olds.get("drift"):
            changed.append("drift")
        # The name is the entity's identity: renaming it makes a new one, and
        # whatever referred to the old name must follow.
        replaces = ["name"] if olds["name"] != news["name"] else []
        return dynamic.DiffResult(changes=bool(changed or replaces), replaces=replaces)

    def update(
        self, id_: str, _olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        client = OpenMetadataClient(news)
        entity = self._get(client, f"{self.collection}/{id_}")
        # An update cannot change the resource's id, so a server_url that points
        # at a different OpenMetadata would otherwise patch a different entity.
        if entity is None:
            msg = (
                f"{self.kind} {id_} is not at {news['server_url']}. It cannot "
                "move between OpenMetadata servers in place; if it was deleted, "
                "refresh."
            )
            raise ValueError(msg)
        self._reconcile(client, entity, news)
        return dynamic.UpdateResult(outs={**news, "drift": []})

    def delete(self, id_: str, props: dict[str, Any]) -> None:
        OpenMetadataClient(props).delete(f"{self.collection}/{id_}", hardDelete="true")


class OpenMetadataPolicyProvider(_AccessControlProvider):
    """CRUD for one OpenMetadata policy."""

    collection = "policies"
    kind = "policy"
    managed: ClassVar[dict[str, str]] = {
        **_AccessControlProvider.managed,
        "rules": "rules",
    }

    def _create_body(
        self, _client: OpenMetadataClient, props: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "name": props["name"],
            "displayName": props["display_name"],
            "description": props["description"],
            "rules": props["rules"],
        }

    def _held(self, entity: dict[str, Any], prop: str) -> Any:
        if prop == "rules":
            return _normalized_rules(entity["rules"])
        return super()._held(entity, prop)

    def _declared(self, props: dict[str, Any], prop: str) -> Any:
        if prop == "rules":
            return _normalized_rules(props["rules"])
        return super()._declared(props, prop)


class OpenMetadataRoleProvider(_AccessControlProvider):
    """CRUD for one OpenMetadata role.

    Deleting a role takes it from every user and team that holds it.
    """

    collection = "roles"
    kind = "role"
    fields = "policies"
    managed: ClassVar[dict[str, str]] = {
        **_AccessControlProvider.managed,
        "policies": "policies",
    }

    def _create_body(
        self, _client: OpenMetadataClient, props: dict[str, Any]
    ) -> dict[str, Any]:
        # CreateRole takes policy names.
        return {
            "name": props["name"],
            "displayName": props["display_name"],
            "description": props["description"],
            "policies": props["policies"],
        }

    def _held(self, entity: dict[str, Any], prop: str) -> Any:
        if prop == "policies":
            return _ref_names(entity["policies"])
        return super()._held(entity, prop)

    def _value(
        self, client: OpenMetadataClient, props: dict[str, Any], prop: str
    ) -> Any:
        if prop == "policies":
            return client.references("policies", "policy", props["policies"])
        return super()._value(client, props, prop)


class OpenMetadataTeamProvider(_AccessControlProvider):
    """CRUD for one OpenMetadata ``Group`` team, leaving its members alone."""

    collection = "teams"
    kind = "team"
    fields = "defaultRoles"
    managed: ClassVar[dict[str, str]] = {
        **_AccessControlProvider.managed,
        "default_roles": "defaultRoles",
    }

    def _create_body(
        self, client: OpenMetadataClient, props: dict[str, Any]
    ) -> dict[str, Any]:
        # With no parents the server puts the team under Organization.
        # isJoinable defaults to true, which would let any user add themselves
        # to a team that owns assets; it is set only here, as the Job did.
        body = {
            "name": props["name"],
            "displayName": props["display_name"],
            "description": props["description"],
            "teamType": _TEAM_TYPE,
            "isJoinable": False,
        }
        if "default_roles" in props:
            # CreateTeam takes role ids.
            body["defaultRoles"] = [
                ref["id"]
                for ref in client.references("roles", "role", props["default_roles"])
            ]
        return body

    def _held(self, entity: dict[str, Any], prop: str) -> Any:
        if prop == "default_roles":
            return _ref_names(entity["defaultRoles"])
        return super()._held(entity, prop)

    def _value(
        self, client: OpenMetadataClient, props: dict[str, Any], prop: str
    ) -> Any:
        if prop == "default_roles":
            return client.references("roles", "role", props["default_roles"])
        return super()._value(client, props, prop)

    def _check(self, entity: dict[str, Any]) -> None:
        # The server refuses to change a Group's type, and a non-Group team
        # cannot own assets, so this needs a person to sort out.
        if entity["teamType"] != _TEAM_TYPE:
            msg = (
                f"team {entity['name']!r} exists with teamType "
                f"{entity['teamType']!r}, expected {_TEAM_TYPE!r}"
            )
            raise ValueError(msg)


def _inputs(connection: OpenMetadataConnection, spec: BaseModel) -> dict[str, Any]:
    # None-valued fields are left out, as Pulumi would drop them anyway.
    return {
        **connection.to_props(),
        **spec.model_dump(exclude_none=True),
        "drift": None,
    }


def _options(opts: pulumi.ResourceOptions | None) -> pulumi.ResourceOptions:
    return pulumi.ResourceOptions.merge(
        opts, pulumi.ResourceOptions(additional_secret_outputs=["client_secret"])
    )


class OpenMetadataPolicy(dynamic.Resource):
    """An OpenMetadata access policy.

    :param name: Pulumi resource name.
    :param connection: The OpenMetadata API and an admin client for it.
    :param spec: The policy's declared state.
    :param opts: Resource options.
    """

    def __init__(
        self,
        name: str,
        connection: OpenMetadataConnection,
        spec: OpenMetadataPolicySpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            OpenMetadataPolicyProvider(),
            name,
            _inputs(connection, spec),
            opts=_options(opts),
        )


class OpenMetadataRole(dynamic.Resource):
    """An OpenMetadata role.

    :param name: Pulumi resource name.
    :param connection: The OpenMetadata API and an admin client for it.
    :param spec: The role's declared state.
    :param opts: Resource options; depend on the role's policies.
    """

    def __init__(
        self,
        name: str,
        connection: OpenMetadataConnection,
        spec: OpenMetadataRoleSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            OpenMetadataRoleProvider(),
            name,
            _inputs(connection, spec),
            opts=_options(opts),
        )


class OpenMetadataTeam(dynamic.Resource):
    """An OpenMetadata ``Group`` team whose members are assigned in the UI.

    :param name: Pulumi resource name.
    :param connection: The OpenMetadata API and an admin client for it.
    :param spec: The team's declared state.
    :param opts: Resource options; depend on the team's default roles.
    """

    def __init__(
        self,
        name: str,
        connection: OpenMetadataConnection,
        spec: OpenMetadataTeamSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            OpenMetadataTeamProvider(),
            name,
            _inputs(connection, spec),
            opts=_options(opts),
        )
