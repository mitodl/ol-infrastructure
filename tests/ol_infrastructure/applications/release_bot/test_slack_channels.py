"""Tests for turning configured channel names into the ids Slack requires."""

import pytest
import slack_channels

_MISSING_SCOPE = "missing_scope"


class _FakeSlackError(Exception):
    """Stand-in for slack_sdk's SlackApiError, which isn't installed here."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"error": code}


class _FakeClient:
    """conversations.list over *pages*, each a list of channel dicts."""

    def __init__(self, pages=None, error=None):
        self.pages = pages if pages is not None else [[]]
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def conversations_list(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise _FakeSlackError(self.error)
        index = 0
        cursor = kwargs.get("cursor") or ""
        if cursor:
            index = int(cursor)
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else ""
        return {
            "channels": self.pages[index],
            "response_metadata": {"next_cursor": next_cursor},
        }


@pytest.fixture(autouse=True)
def _reset_module_state():
    slack_channels._name_to_id.clear()
    slack_channels._last_refresh = None
    slack_channels._listing_disabled = False
    yield
    slack_channels._name_to_id.clear()
    slack_channels._last_refresh = None
    slack_channels._listing_disabled = False


async def test_a_name_resolves_to_an_id():
    """The whole point: chat.postMessage rejects the configured name."""
    client = _FakeClient([[{"id": "C123", "name": "product-mit-learn"}]])

    resolved = await slack_channels.resolve(client, "product-mit-learn")

    assert resolved == "C123"


async def test_an_id_is_passed_through_without_an_api_call():
    """Config may already hold an id; there is nothing to look up."""
    client = _FakeClient()

    assert await slack_channels.resolve(client, "C0123ABCD") == "C0123ABCD"
    assert client.calls == []


async def test_a_leading_hash_and_case_are_tolerated():
    client = _FakeClient([[{"id": "C123", "name": "product-xpro"}]])

    assert await slack_channels.resolve(client, "#Product-XPro") == "C123"


async def test_the_listing_is_paginated():
    """A workspace past one page would otherwise silently lose channels."""
    client = _FakeClient(
        [
            [{"id": "C1", "name": "product-one"}],
            [{"id": "C2", "name": "product-two"}],
        ]
    )

    assert await slack_channels.resolve(client, "product-two") == "C2"


async def test_resolution_is_cached():
    """These run on 60s and 120s poll loops; one list call must serve them."""
    client = _FakeClient([[{"id": "C123", "name": "product-ovs"}]])

    await slack_channels.resolve(client, "product-ovs")
    await slack_channels.resolve(client, "product-ovs")

    assert len(client.calls) == 1


async def test_an_unknown_name_does_not_relist_on_every_call():
    """A wrong name or an un-invited channel must not cost a call per poll."""
    client = _FakeClient([[{"id": "C123", "name": "product-ovs"}]])

    await slack_channels.resolve(client, "product-typo")
    await slack_channels.resolve(client, "product-typo")

    assert len(client.calls) == 1


async def test_an_unknown_name_falls_back_to_the_input():
    """Returning the name keeps the caller's own error path intact."""
    client = _FakeClient([[{"id": "C123", "name": "product-ovs"}]])

    assert await slack_channels.resolve(client, "product-typo") == "product-typo"


async def test_missing_scope_stops_further_listing():
    """Without groups:read every call fails identically -- stop asking."""
    client = _FakeClient(error=_MISSING_SCOPE)

    first = await slack_channels.resolve(client, "product-ovs")
    second = await slack_channels.resolve(client, "product-xpro")

    assert first == "product-ovs"
    assert second == "product-xpro"
    assert len(client.calls) == 1
    assert slack_channels._listing_disabled


async def test_a_failed_refresh_keeps_previously_resolved_ids():
    """One bad refresh must not drop ids that are already known good."""
    client = _FakeClient([[{"id": "C123", "name": "product-ovs"}]])
    await slack_channels.resolve(client, "product-ovs")

    slack_channels._last_refresh = None
    client.error = "ratelimited"

    assert await slack_channels.resolve(client, "product-ovs") == "C123"


async def test_the_private_channel_type_is_requested_by_default():
    """Asking for public_channel too would need channels:read and 400 the call."""
    client = _FakeClient([[]])

    await slack_channels.resolve(client, "product-ovs")

    assert client.calls[0]["types"] == "private_channel"


async def test_unresolvable_names_the_channels_that_cannot_be_reached():
    client = _FakeClient([[{"id": "C123", "name": "product-ovs"}]])

    missing = await slack_channels.unresolvable(
        client, {"product-ovs", "product-typo", "C0123ABCD"}
    )

    assert missing == ["product-typo"]
