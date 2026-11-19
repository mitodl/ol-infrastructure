"""Tests for resolving commit-author emails to Slack mentions."""

import pytest
import slack_users

_NOT_FOUND = "users_not_found"
_NO_SUCH_USER = "user_not_found"  # what users.info says, vs users.lookupByEmail


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
    slack_users._overrides.clear()
    slack_users._lookups_disabled = False
    yield
    slack_users._cache.clear()
    slack_users._overrides.clear()
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


async def test_an_override_mentions_an_author_with_an_unmatchable_commit_email():
    """The bug this module's override list exists for.

    dsubak commits as `dsubak@users.noreply.github.com`, an address no Slack
    account can hold, so `users.lookupByEmail` is guaranteed to miss and the
    person who needed the ping got a backticked handle instead.
    """
    client = _FakeClient()
    slack_users.configure_overrides({"dsubak": "UTESTUSER01"})

    rendered = await slack_users.mention(client, "dsubak@users.noreply.github.com")

    assert rendered == "<@UTESTUSER01>"
    # Nothing to ask Slack: the answer was configured, not discovered.
    assert client.lookups == []


async def test_an_override_matches_both_github_noreply_forms():
    """The numeric-prefixed and legacy no-reply addresses are one identity."""
    client = _FakeClient()
    slack_users.configure_overrides({"Anas12091101": "UTESTUSER02"})

    assert (
        await slack_users.mention(
            client, "88967643+Anas12091101@users.noreply.github.com"
        )
        == "<@UTESTUSER02>"
    )
    assert (
        await slack_users.mention(client, "Anas12091101@users.noreply.github.com")
        == "<@UTESTUSER02>"
    )


async def test_an_override_can_be_keyed_on_a_personal_commit_address():
    """Not every unmatchable address is a no-reply one."""
    client = _FakeClient()
    slack_users.configure_overrides({"james@jkachel.com": "UTESTUSER03"})

    assert await slack_users.mention(client, "james@jkachel.com") == "<@UTESTUSER03>"


async def test_override_keys_and_values_are_matched_case_insensitively():
    """A handle's casing in git need not match its casing in the config."""
    client = _FakeClient()
    slack_users.configure_overrides({"ArSlAnAsHrAf7": "wtestuser04"})

    assert (
        await slack_users.mention(
            client, "34372316+arslanashraf7@users.noreply.github.com"
        )
        == "<@WTESTUSER04>"
    )


async def test_an_override_wins_over_the_email_lookup():
    """Listing someone is a statement, not a hint; it is not a last resort.

    Someone whose Slack lookup resolves to a stale or duplicate account is
    exactly who an override has to be able to correct.
    """
    client = _FakeClient({"alice@example.com": {"id": "USTALE0001"}})
    slack_users.configure_overrides({"alice@example.com": "UCURRENT01"})

    assert await slack_users.mention(client, "alice@example.com") == "<@UCURRENT01>"
    assert client.lookups == []


async def test_overrides_still_resolve_without_the_email_lookup_scope():
    """A missing users:read.email scope must not disable the override list.

    The two failure modes are independent: overrides need no API call at all,
    so the one case where the bot can mention *nobody* by email is the case
    where the configured mentions matter most.
    """
    client = _FakeClient(error="missing_scope")
    slack_users.configure_overrides({"dsubak": "UTESTUSER01"})

    assert await slack_users.mention(client, "alice@example.com") == "`alice`"
    assert slack_users._lookups_disabled
    assert (
        await slack_users.mention(client, "dsubak@users.noreply.github.com")
        == "<@UTESTUSER01>"
    )


async def test_an_unlisted_author_is_unaffected_by_the_override_list():
    client = _FakeClient({"alice@example.com": {"id": "U1"}})
    slack_users.configure_overrides({"dsubak": "UTESTUSER01"})

    assert await slack_users.mention(client, "alice@example.com") == "<@U1>"
    assert await slack_users.mention(client, "bob@example.com") == "`bob`"


async def test_overrides_that_are_not_slack_ids_are_dropped():
    """A display name or email in the value renders a mention that pings nobody.

    `<@Daniel Subak>` is not a mention; it is text that looks like one, which
    is the whole failure this list exists to end.
    """
    client = _FakeClient()
    slack_users.configure_overrides(
        {
            "dsubak": "Daniel Subak",
            "annagav": "annagav@mit.edu",
            "quazi-h": "",
            "shaidar": "WTESTUSER05",
        }
    )

    assert slack_users._overrides == {"shaidar": "WTESTUSER05"}
    assert (
        await slack_users.mention(client, "dsubak@users.noreply.github.com")
        == "`dsubak`"
    )


async def test_a_leading_at_sign_in_an_override_value_is_tolerated():
    """Copying an id out of Slack can bring an "@" along with it."""
    client = _FakeClient()
    slack_users.configure_overrides({"dsubak": "@UTESTUSER01"})

    assert (
        await slack_users.mention(client, "dsubak@users.noreply.github.com")
        == "<@UTESTUSER01>"
    )


async def test_configuring_overrides_clears_a_stale_cached_fallback():
    """A hit is cached for a day -- longer than it should take to fix the list."""
    client = _FakeClient()

    assert (
        await slack_users.mention(client, "dsubak@users.noreply.github.com")
        == "`dsubak`"
    )
    slack_users.configure_overrides({"dsubak": "UTESTUSER01"})

    assert (
        await slack_users.mention(client, "dsubak@users.noreply.github.com")
        == "<@UTESTUSER01>"
    )


async def test_an_override_also_beats_the_bot_author_filter():
    """An explicit entry outranks every heuristic, including the bot pattern.

    A human whose handle happens to match `_BOT_AUTHOR_RE` must still be
    mentionable once someone says so.
    """
    client = _FakeClient()
    slack_users.configure_overrides({"renovate@example.com": "UTESTUSER01"})

    assert await slack_users.mention(client, "renovate@example.com") == "<@UTESTUSER01>"


async def test_format_authors_mixes_overrides_and_email_lookups():
    """The real "Still waiting on:" line has both kinds of author in it."""
    client = _FakeClient({"jkachel@mit.edu": {"id": "UTESTUSER03"}})
    slack_users.configure_overrides({"dsubak": "UTESTUSER01"})

    rendered = await slack_users.format_authors(
        client,
        {
            "dsubak@users.noreply.github.com",
            "jkachel@mit.edu",
            "nobody@example.com",
        },
    )

    assert rendered == "<@UTESTUSER01>, <@UTESTUSER03>, `nobody`"


class _FakeUsersInfoClient(_FakeClient):
    def __init__(self, infos=None, error=None):
        super().__init__()
        self.infos = infos or {}
        self.info_error = error
        self.info_calls: list[str] = []

    async def users_info(self, user):
        self.info_calls.append(user)
        if self.info_error:
            raise _FakeSlackError(self.info_error)
        if user not in self.infos:
            raise _FakeSlackError(_NO_SUCH_USER)
        return {"user": self.infos[user]}


async def test_unusable_overrides_names_ids_that_cannot_be_mentioned():
    """A mistyped or stale id fails exactly like the bug it was added to fix."""
    client = _FakeUsersInfoClient(
        {
            "UTESTUSER01": {"id": "UTESTUSER01"},
            "UDEPARTED1": {"id": "UDEPARTED1", "deleted": True},
        }
    )
    slack_users.configure_overrides(
        {"dsubak": "UTESTUSER01", "departed": "UDEPARTED1", "typo": "UNOSUCHID1"}
    )

    unusable = await slack_users.unusable_overrides(client)

    assert len(unusable) == 2
    assert any(
        entry.startswith("departed -> UDEPARTED1 (deactivated") for entry in unusable
    )
    assert any(
        entry.startswith("typo -> UNOSUCHID1 (user_not_found") for entry in unusable
    )
    assert not any("dsubak" in entry for entry in unusable)


async def test_unusable_overrides_does_not_complain_without_the_scope():
    """Not being able to check says nothing about whether the ids are right."""
    client = _FakeUsersInfoClient(error="missing_scope")
    slack_users.configure_overrides({"dsubak": "UTESTUSER01", "shaidar": "WTESTUSER05"})

    assert await slack_users.unusable_overrides(client) == []
    # Stopped after the first refusal rather than asking once per entry.
    assert len(client.info_calls) == 1
