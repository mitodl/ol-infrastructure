"""Pulumi dynamic provider for OpenMetadata alerts (event subscriptions).

``PUT /v1/events/subscriptions`` creates the subscription or updates the one
with the same name, so an alert made by hand under Settings > Alerts with a
declared name is adopted. Creating one needs ``Create`` on ``eventsubscription``,
which no bot policy grants, so these connect with the same admin client as the
bots (see client.py).

The server compiles ``input`` into the filtering rules it evaluates, against
the filters and actions OpenMetadata lists for the resource
(EntityObservabilityFilterDescriptor.json for an ``Observability`` alert). A
filter it does not list for the resource is a 400, so a rule cannot be written
directly: a PATCH of ``filteringRules`` is rebuilt from ``input`` on update.

Slack is delivered through an incoming webhook only; OpenMetadata 2.0.6 has no
bot-token Slack destination.

``read`` reports whether the server still holds the declared input, destination
and enabled flag, so an alert edited or disabled in the UI is put back on the
next ``pulumi up --refresh``.
"""

from typing import Any, Literal

import pulumi
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict

from .client import OpenMetadataClient, OpenMetadataConnection

_CONNECTION_PROPS = ("server_url", "token_url", "client_id", "client_secret")
_UPDATABLE_PROPS = (
    "display_name",
    "description",
    "alert_type",
    "resource",
    "filters",
    "actions",
    "slack_webhook_url",
)


class AlertArgument(BaseModel):
    """One named argument of an alert filter or action.

    :param name: The argument's name in the descriptor, e.g.
        ``contractStatusList``.
    :param input: Its values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    input: list[str]


class AlertCondition(BaseModel):
    """A filter or action from the resource's descriptor, with its arguments.

    Every field is set explicitly, because the server fills in the defaults and
    ``read`` compares what it returns with what is declared.

    :param name: The descriptor entry, e.g. ``GetDataContractStatusUpdates``.
    :param effect: Whether a match includes or excludes the event.
    :param prefix_condition: How this joins the conditions before it.
    :param arguments: The entry's arguments; empty for one with none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    effect: Literal["include", "exclude"] = "include"
    prefix_condition: Literal["AND", "OR"] = "AND"
    arguments: list[AlertArgument] = []

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "effect": self.effect,
            "prefixCondition": self.prefix_condition,
            "arguments": [argument.model_dump() for argument in self.arguments],
        }


class OpenMetadataEventSubscriptionSpec(BaseModel):
    """Declared state of one alert.

    :param name: The alert's name.
    :param display_name: Shown in the OpenMetadata UI.
    :param description: Shown on the alert's page.
    :param alert_type: ``Observability`` for test case and contract results,
        ``Notification`` for entity changes.
    :param resource: The one entity type the alert watches, e.g. ``dataContract``.
    :param filters: Which events of that resource are considered.
    :param actions: Which status changes trigger it (``Observability`` only).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str
    description: str
    alert_type: Literal["Observability", "Notification"]
    resource: str
    filters: list[AlertCondition] = []
    actions: list[AlertCondition] = []


def _input(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "filters": [AlertCondition(**f).to_api() for f in props["filters"]],
        "actions": [AlertCondition(**a).to_api() for a in props["actions"]],
    }


def _destination(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": "External",
        "type": "Slack",
        "config": {"endpoint": props["slack_webhook_url"]},
    }


def _apply(client: OpenMetadataClient, props: dict[str, Any]) -> dict[str, Any]:
    return client.put(
        "events/subscriptions",
        {
            "name": props["name"],
            "displayName": props["display_name"],
            "description": props["description"],
            "alertType": props["alert_type"],
            "resources": [props["resource"]],
            "enabled": True,
            "input": _input(props),
            "destinations": [_destination(props)],
        },
    )


def _normalized_conditions(conditions: list[dict[str, Any]]) -> list[Any]:
    return sorted(
        (
            c["name"],
            c.get("effect", "include"),
            c.get("prefixCondition", "AND"),
            sorted(
                (argument["name"], sorted(argument["input"]))
                for argument in c.get("arguments") or []
            ),
        )
        for c in conditions
    )


def _in_sync(subscription: dict[str, Any], props: dict[str, Any]) -> bool:
    held_input = subscription.get("input") or {}
    declared_input = _input(props)
    destinations = [
        (d["category"], d["type"], (d.get("config") or {}).get("endpoint"))
        for d in subscription.get("destinations") or []
    ]
    return (
        subscription.get("enabled") is True
        and subscription.get("alertType") == props["alert_type"]
        and (subscription.get("filteringRules") or {}).get("resources")
        == [props["resource"]]
        and subscription.get("displayName") == props["display_name"]
        and subscription.get("description") == props["description"]
        and destinations == [("External", "Slack", props["slack_webhook_url"])]
        and all(
            _normalized_conditions(held_input.get(key) or [])
            == _normalized_conditions(declared_input[key])
            for key in ("filters", "actions")
        )
    )


class OpenMetadataEventSubscriptionProvider(dynamic.ResourceProvider):
    """CRUD for one OpenMetadata alert."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        subscription = _apply(OpenMetadataClient(props), props)
        return dynamic.CreateResult(
            id_=subscription["id"], outs={**props, "in_sync": True}
        )

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        subscription = OpenMetadataClient(props).get(f"events/subscriptions/{id_}")
        if subscription is None:
            # An empty id tells the engine the alert is gone, so one deleted in
            # the UI is created again on the next update.
            return dynamic.ReadResult(id_="", outs={})
        return dynamic.ReadResult(
            id_=id_, outs={**props, "in_sync": _in_sync(subscription, props)}
        )

    def diff(
        self, _id: str, olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.DiffResult:
        changed = [
            key
            for key in (*_CONNECTION_PROPS, *_UPDATABLE_PROPS)
            if olds.get(key) != news[key]
        ]
        if olds.get("in_sync") is False:
            changed.append("in_sync")
        # The PUT is keyed on name, so a renamed alert is a new one.
        replaces = ["name"] if olds["name"] != news["name"] else []
        return dynamic.DiffResult(changes=bool(changed or replaces), replaces=replaces)

    def update(
        self, id_: str, _olds: dict[str, Any], news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        client = OpenMetadataClient(news)
        # Checked before the PUT, which matches by name: a server_url pointing
        # at another OpenMetadata would otherwise overwrite that server's alert
        # of the same name before the mismatch could be noticed.
        if client.get(f"events/subscriptions/{id_}") is None:
            msg = (
                f"alert {id_} is not at {news['server_url']}. An alert cannot "
                "move between OpenMetadata servers in place; if it was deleted, "
                "refresh."
            )
            raise ValueError(msg)
        _apply(client, news)
        return dynamic.UpdateResult(outs={**news, "in_sync": True})

    def delete(self, id_: str, props: dict[str, Any]) -> None:
        OpenMetadataClient(props).delete(
            f"events/subscriptions/{id_}", hardDelete="true"
        )


class OpenMetadataEventSubscription(dynamic.Resource):
    """An OpenMetadata alert that posts to one Slack incoming webhook.

    :param name: Pulumi resource name.
    :param connection: The OpenMetadata API and an admin client for it.
    :param spec: The alert's declared state.
    :param slack_webhook_url: The incoming webhook the alert posts to.
    :param opts: Resource options.
    """

    def __init__(
        self,
        name: str,
        connection: OpenMetadataConnection,
        spec: OpenMetadataEventSubscriptionSpec,
        slack_webhook_url: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            OpenMetadataEventSubscriptionProvider(),
            name,
            {
                **connection.to_props(),
                **spec.model_dump(),
                "slack_webhook_url": pulumi.Output.secret(slack_webhook_url),
                "in_sync": None,
            },
            opts=pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(
                    additional_secret_outputs=["slack_webhook_url", "client_secret"]
                ),
            ),
        )
