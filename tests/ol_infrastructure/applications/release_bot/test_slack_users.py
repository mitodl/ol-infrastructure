"""Tests for resolving commit-author emails to Slack mentions."""

import pytest
import slack_users

_NOT_FOUND = "users_not_found"


class _FakeSlackError(Exception):
    """Stand-in for slack_sdk's SlackApiError, which isn't installed here."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"error": code}


class _FakeClient:
    def __init__(self, users=None, error=None):
        self.users = users or {}
        self.error = error
        self.lookups: list[str] = []

    async def users_lookupByEmail(self, email):  # noqa: N802
        self.lookups.append(email)
        if self.error:
            raise _FakeSlackError(self.error)
        if email not in self.users:
            raise _FakeSlackError(_NOT_FOUND)
        return {"user": self.users[email]}


@pytest.fixture(autouse=True)
def _reset_module_state():
    slack_users._cache.clear()
    slack_users._lookups_disabled = False
    yield
    slack_users._cache.clear()
    slack_users._lookups_disabled = False


async def test_email_resolves_to_a_real_mention():
    """The whole point: `<@U…>` notifies, a pasted email does not."""
    client = _FakeClient({"alice@example.com": {"id": "U1"}})

    assert await slack_users.mention(client, "alice@example.com") == "<@U1>"


async def test_lookups_are_cached():
    """The checkbox watcher polls every 60s; re-resolving each cycle is waste."""
    client = _FakeClient({"alice@example.com": {"id": "U1"}})

    await slack_users.mention(client, "alice@example.com")
    await slack_users.mention(client, "alice@example.com")

    assert client.lookups == ["alice@example.com"]


async def test_unknown_email_is_not_leaked_into_the_channel():
    """A failed lookup falls back to a handle, never the full address."""
    client = _FakeClient()

    rendered = await slack_users.mention(client, "alice@example.com")

    assert rendered == "`alice`"
    assert "@example.com" not in rendered


async def test_github_noreply_addresses_render_as_the_github_handle():
    """`12345+handle@users.noreply.github.com` carries a usable identity."""
    client = _FakeClient()

    assert (
        await slack_users.mention(client, "1234+octocat@users.noreply.github.com")
        == "`octocat`"
    )
    assert (
        await slack_users.mention(client, "hubot@users.noreply.github.com") == "`hubot`"
    )


async def test_bot_authors_are_never_looked_up():
    """Renovate has no Slack account and must never be pinged or searched for."""
    client = _FakeClient()

    await slack_users.mention(client, "29139614+renovate[bot]@users.noreply.github.com")
    await slack_users.mention(client, "renovate@whitesourcesoftware.com")

    assert client.lookups == []


async def test_missing_scope_disables_further_lookups():
    """Without users:read.email every lookup fails identically -- stop asking.

    The watcher polls per author per minute; hammering a scope error would
    burn rate limit for a result that cannot change until the token is
    reinstalled.
    """
    client = _FakeClient(error="missing_scope")

    first = await slack_users.mention(client, "alice@example.com")
    second = await slack_users.mention(client, "bob@example.com")

    assert first == "`alice`"
    assert second == "`bob`"
    assert client.lookups == ["alice@example.com"]
    assert slack_users._lookups_disabled


async def test_deactivated_accounts_are_not_mentioned():
    """Mentioning a deleted account notifies nobody while looking like it did."""
    client = _FakeClient({"alice@example.com": {"id": "U1", "deleted": True}})

    assert await slack_users.mention(client, "alice@example.com") == "`alice`"


async def test_format_authors_renders_a_sorted_mention_list():
    client = _FakeClient(
        {"alice@example.com": {"id": "U1"}, "bob@example.com": {"id": "U2"}}
    )

    rendered = await slack_users.format_authors(
        client, {"bob@example.com", "alice@example.com"}
    )

    assert rendered == "<@U1>, <@U2>"


async def test_a_failed_lookup_is_retried_sooner_than_a_successful_one(monkeypatch):
    """Someone who joins Slack should get pinged without a pod restart."""
    client = _FakeClient()
    now = [1000.0]
    monkeypatch.setattr(slack_users.time, "monotonic", lambda: now[0])

    await slack_users.mention(client, "alice@example.com")
    now[0] += slack_users._MISS_TTL_SECONDS + 1
    client.users["alice@example.com"] = {"id": "U1"}

    assert await slack_users.mention(client, "alice@example.com") == "<@U1>"


async def test_malformed_addresses_are_still_redacted():
    """A git author field is an arbitrary string, not a valid address.

    Anything with an "@" is truncated at the first one whether or not it
    parses as an email, so a malformed address cannot post itself in full.
    """
    client = _FakeClient()

    assert await slack_users.mention(client, "alice@@example.com") == "`alice`"
    assert await slack_users.mention(client, '"alice@work"@example.com') == '`"alice`'
    assert client.lookups == []


async def test_a_bare_name_is_left_alone():
    """Not every author string is address-shaped; one without "@" survives."""
    client = _FakeClient()

    assert await slack_users.mention(client, "Alice Example") == "`Alice Example`"
