"""Tests for release_bot's Slack command handlers.

The handlers take their Slack callables (ack/respond) as arguments, so they
can be driven directly with async stubs -- no Slack app or socket needed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import bot
import bot_config
import pytest
import slack_users


@pytest.fixture
def repos():
    return {
        "my-app": bot_config.AppConfig(
            pipeline="my-app-pipeline",
            repo="mitodl/my-app",
            branch="main",
            channel="C123",
            release_workflow=True,
        )
    }


@pytest.fixture
def mixed_repos(repos):
    """One migrated app alongside one still on the legacy pipeline."""
    return dict(
        repos,
        **{
            "legacy-app": bot_config.AppConfig(
                pipeline="legacy-app-pipeline",
                repo="mitodl/legacy-app",
                branch="master",
                channel="C456",
            )
        },
    )


@pytest.fixture
def slack():
    """Return (ack, respond) stubs plus a helper to read what was said."""

    class _Slack:
        def __init__(self):
            self.ack = AsyncMock()
            self.respond = AsyncMock()

        @property
        def said(self) -> str:
            return "\n".join(
                str(call.args[0]) for call in self.respond.call_args_list if call.args
            )

    return _Slack()


def _command(text: str) -> dict[str, str]:
    return {"text": text}


def _record_trigger(calls: list[tuple[str, str, str]]):
    """Record the trigger call and return the build URL the handler reports."""

    def _trigger(pipeline, job):
        calls.append(("trigger", pipeline, job))
        return "http://build/1"

    return _trigger


@pytest.fixture(autouse=True)
def _no_pending_hotfix(monkeypatch):
    """Default to no pending hotfix, since one changes what /doof release does."""
    monkeypatch.setattr(
        bot.github, "pending_hotfix_requests", AsyncMock(return_value=[])
    )


# ---------------------------------------------------------------------------
# /doof release
# ---------------------------------------------------------------------------


async def test_release_checks_the_resource_before_triggering(repos, slack, monkeypatch):
    """The check must happen first, or the build binds a stale version.

    The release resource is `check_every: never` with no webhook, so Concourse
    only checks it when asked. Triggering the job without checking made the
    new release reuse the *previous* release's version number and commit list
    -- the "why is it still 2026.8.3.1" symptom.
    """
    calls = []
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))
    monkeypatch.setattr(
        bot.concourse,
        "check_resource",
        AsyncMock(side_effect=lambda p, r: calls.append(("check", p, r))),
    )
    monkeypatch.setattr(
        bot.concourse,
        "trigger_job",
        AsyncMock(side_effect=_record_trigger(calls)),
    )

    await bot._cmd_release(repos, slack.ack, slack.respond, _command("my-app"), {})

    assert calls == [
        ("check", "my-app-pipeline", "my-app-release"),
        ("trigger", "my-app-pipeline", "build-my-app-release-image"),
    ]
    assert "Release triggered" in slack.said


async def test_release_refuses_to_trigger_when_the_check_fails(
    repos, slack, monkeypatch
):
    """A failed check means a stale version -- better to stop than release it."""
    trigger = AsyncMock()
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))
    monkeypatch.setattr(
        bot.concourse, "check_resource", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_release(repos, slack.ack, slack.respond, _command("my-app"), {})

    trigger.assert_not_called()
    assert "Refusing to trigger" in slack.said


async def test_release_warns_that_it_supersedes_an_in_flight_release(
    repos, slack, monkeypatch
):
    """Superseding deletes a branch and tag -- never do that silently."""
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(
            return_value={
                "version": "2026.8.3.1",
                "branch": "releases/2026.8.3.1",
                "url": "https://github.com/mitodl/my-app/tree/releases/2026.8.3.1",
                "cut_at": datetime.now(tz=UTC) - timedelta(days=9),
            }
        ),
    )
    monkeypatch.setattr(bot.concourse, "check_resource", AsyncMock())
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(return_value="http://build/1")
    )

    await bot._cmd_release(repos, slack.ack, slack.respond, _command("my-app"), {})

    said = slack.said
    assert "Superseding in-flight release" in said
    assert "2026.8.3.1" in said
    assert "cut 9d ago" in said


async def test_release_still_triggers_when_the_in_flight_lookup_fails(
    repos, slack, monkeypatch
):
    """The in-flight report decorates the message; it must not gate the release."""
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(side_effect=RuntimeError("github down")),
    )
    monkeypatch.setattr(bot.concourse, "check_resource", AsyncMock())
    trigger = AsyncMock(return_value="http://build/1")
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_release(repos, slack.ack, slack.respond, _command("my-app"), {})

    trigger.assert_awaited_once()
    assert "Release triggered" in slack.said


async def test_release_rejects_an_unknown_app(repos, slack, monkeypatch):
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)
    await bot._cmd_release(repos, slack.ack, slack.respond, _command("nope"), {})
    trigger.assert_not_called()
    assert "Unknown app" in slack.said


# ---------------------------------------------------------------------------
# /doof preview
# ---------------------------------------------------------------------------


async def test_preview_reports_the_next_version_and_its_commits(
    repos, slack, monkeypatch
):
    monkeypatch.setattr(
        bot.github,
        "release_preview",
        AsyncMock(
            return_value={
                "version": "2026.8.12.1",
                "since": "2026.8.3.1",
                "commits": [
                    {
                        "sha": "abc12345678",  # pragma: allowlist secret
                        "message": "fix: a bug\n\nbody",
                        "author": "dev",
                    }
                ],
                "in_flight": None,
            }
        ),
    )

    await bot._cmd_preview(repos, slack.ack, slack.respond, _command("my-app"), {})

    said = slack.said
    assert "2026.8.12.1" in said
    assert "1 commit(s) since 2026.8.3.1" in said
    assert "fix: a bug" in said
    # Only the subject, not the body.
    assert "body" not in said.replace("fix: a bug", "")


async def test_preview_triggers_nothing(repos, slack, monkeypatch):
    """The whole point of preview: it must have no side effects."""
    check = AsyncMock()
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "check_resource", check)
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)
    monkeypatch.setattr(
        bot.github,
        "release_preview",
        AsyncMock(
            return_value={
                "version": "2026.8.12.1",
                "since": "2026.8.3.1",
                "commits": [],
                "in_flight": None,
            }
        ),
    )

    await bot._cmd_preview(repos, slack.ack, slack.respond, _command("my-app"), {})

    check.assert_not_called()
    trigger.assert_not_called()
    assert "Nothing to release" in slack.said
    assert "nothing was triggered" in slack.said


async def test_preview_flags_an_in_flight_release(repos, slack, monkeypatch):
    monkeypatch.setattr(
        bot.github,
        "release_preview",
        AsyncMock(
            return_value={
                "version": "2026.8.12.1",
                "since": "2026.8.3.1",
                "commits": [],
                "in_flight": {
                    "version": "2026.8.3.1",
                    "branch": "releases/2026.8.3.1",
                    "url": "https://github.com/mitodl/my-app/tree/releases/2026.8.3.1",
                    "cut_at": None,
                },
            }
        ),
    )

    await bot._cmd_preview(repos, slack.ack, slack.respond, _command("my-app"), {})
    assert "In flight" in slack.said
    assert "supersedes it" in slack.said


async def test_preview_reports_a_pending_hotfix_instead_of_the_normal_next_version(
    repos, slack, monkeypatch
):
    """A pending hotfix is what the resource would actually offer next.

    Without this, preview reports the calver-next version and every commit
    on the default branch -- contradicting what `/doof release` would cut
    (and refuse to), since the release resource offers the hotfix instead.
    """
    release_preview = AsyncMock()
    monkeypatch.setattr(bot.github, "release_preview", release_preview)
    monkeypatch.setattr(
        bot.github, "pending_hotfix_requests", AsyncMock(return_value=[_SHA])
    )

    await bot._cmd_preview(repos, slack.ack, slack.respond, _command("my-app"), {})

    release_preview.assert_not_awaited()
    said = slack.said
    assert "a hotfix of `aaaaaaa` is pending" in said
    assert "not a normal release" in said
    assert "/doof hotfix my-app aaaaaaa" in said


async def test_preview_fails_closed_when_the_pending_hotfix_check_errors(
    repos, slack, monkeypatch
):
    release_preview = AsyncMock()
    monkeypatch.setattr(bot.github, "release_preview", release_preview)
    monkeypatch.setattr(
        bot.github,
        "pending_hotfix_requests",
        AsyncMock(side_effect=RuntimeError("github down")),
    )

    await bot._cmd_preview(repos, slack.ack, slack.respond, _command("my-app"), {})

    release_preview.assert_not_awaited()
    assert "Could not check" in slack.said


# ---------------------------------------------------------------------------
# /doof release-status
# ---------------------------------------------------------------------------


async def test_release_status_surfaces_a_stuck_release(repos, slack, monkeypatch):
    """A releases/ branch outliving production is the failure to surface."""
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(
            return_value={
                "version": "2026.8.3.1",
                "branch": "releases/2026.8.3.1",
                "url": "https://github.com/mitodl/my-app/tree/releases/2026.8.3.1",
                "cut_at": datetime.now(tz=UTC) - timedelta(days=9),
            }
        ),
    )

    await bot._cmd_release_status(repos, slack.ack, slack.respond, _command(""), {})

    said = slack.said
    assert "in flight" in said
    assert "2026.8.3.1" in said


async def test_release_status_reports_checklist_progress(repos, slack, monkeypatch):
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app 2026.8.12.1",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": "- [x] **A** (#1) by a@x\n- [ ] **B** (#2) by b@x\n",
                    "labels": ["release"],
                }
            ]
        ),
    )
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))

    await bot._cmd_release_status(repos, slack.ack, slack.respond, _command(""), {})
    assert "1/2 checked" in slack.said


# ---------------------------------------------------------------------------
# /doof wait-for-checkboxes
# ---------------------------------------------------------------------------


_NOT_FOUND = "users_not_found"


def _slack_client(users=None):
    """Return a Slack client resolving *users* by email and nothing else."""

    class _Error(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"error": code}

    resolved = users or {}

    async def _lookup(email):
        if email not in resolved:
            raise _Error(_NOT_FOUND)
        return {"user": {"id": resolved[email]}}

    client = MagicMock()
    client.users_lookupByEmail = _lookup
    client.chat_postMessage = AsyncMock()
    return client


@pytest.fixture(autouse=True)
def _clear_watchers(monkeypatch):
    """Reset the module-global watcher state so it cannot leak between tests.

    `_slack_app` is set here because create_app() always populates it before
    the socket handler starts accepting commands, so a handler observing it
    unset is unreachable in the running bot. Its client resolves the fixture
    authors to Slack ids so mention rendering is exercised rather than stubbed.
    """
    app = MagicMock()
    app.client = _slack_client(
        {
            "alice@example.com": "UALICE",
            "bob@example.com": "UBOB",
            "carol@example.com": "UCAROL",
        }
    )
    monkeypatch.setattr(bot, "_slack_app", app)
    bot._checkbox_watchers.clear()
    bot._release_requesters.clear()
    bot._app_locks.clear()
    slack_users._cache.clear()
    slack_users._lookups_disabled = False
    yield
    bot._checkbox_watchers.clear()
    bot._release_requesters.clear()
    bot._app_locks.clear()
    slack_users._cache.clear()
    slack_users._lookups_disabled = False


async def test_wait_for_checkboxes_names_who_is_outstanding(repos, slack, monkeypatch):
    """Naming the people is the point -- an unchecked count chases nobody."""
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": (
                        "- [x] **A** (#1) by alice@example.com\n"
                        "- [ ] **B** (#2) by bob@example.com\n"
                        "- [ ] **C** (#3) by carol@example.com\n"
                    ),
                    "labels": ["release"],
                }
            ]
        ),
    )
    started: list[object] = []

    def _capture(coro):
        started.append(coro)
        coro.close()

    monkeypatch.setattr(bot.asyncio, "create_task", _capture)

    await bot._cmd_wait_for_checkboxes(
        repos, slack.ack, slack.respond, _command("my-app"), {}
    )

    said = slack.said
    assert "1/3 checked" in said
    # Mentions, not the raw commit emails: only `<@U…>` notifies anyone.
    assert "<@UBOB>, <@UCAROL>" in said
    assert "bob@example.com" not in said
    assert "<@UALICE>" not in said
    assert len(started) == 1


async def test_wait_for_checkboxes_short_circuits_when_already_complete(
    repos, slack, monkeypatch
):
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": "- [x] **A** (#1) by alice@example.com\n",
                    "labels": ["release"],
                }
            ]
        ),
    )
    started: list[object] = []
    monkeypatch.setattr(bot.asyncio, "create_task", started.append)

    await bot._cmd_wait_for_checkboxes(
        repos, slack.ack, slack.respond, _command("my-app"), {}
    )

    assert "already checked off" in slack.said
    assert not started


async def test_wait_for_checkboxes_does_not_start_a_second_watcher(
    repos, slack, monkeypatch
):
    """A duplicate watcher would post every progress message twice."""
    bot._checkbox_watchers.add("my-app")
    started: list[object] = []
    monkeypatch.setattr(bot.asyncio, "create_task", started.append)
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock())

    await bot._cmd_wait_for_checkboxes(
        repos, slack.ack, slack.respond, _command("my-app"), {}
    )

    assert "Already watching" in slack.said
    assert not started


async def test_wait_for_checkboxes_releases_its_slot_when_nothing_starts(
    repos, slack, monkeypatch
):
    """A preflight reservation must not leak when no watcher is started.

    The slot is claimed before the GitHub lookup so two concurrent invocations
    cannot both pass; every path that then declines to start a watcher has to
    give it back, or the app is locked out until the process restarts.
    """
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    started: list[object] = []
    monkeypatch.setattr(bot.asyncio, "create_task", started.append)

    await bot._cmd_wait_for_checkboxes(
        repos, slack.ack, slack.respond, _command("my-app"), {}
    )

    assert not started
    assert "my-app" not in bot._checkbox_watchers, (
        "The reservation must be released when no watcher was started"
    )


async def test_wait_for_checkboxes_claims_its_slot_before_awaiting(
    repos, slack, monkeypatch
):
    """The slot must be taken before the first await, or the guard is a TOCTOU.

    Two concurrent Slack invocations would otherwise both pass preflight,
    interleave at open_release_issues, and each start a watcher -- posting
    every progress message twice.
    """
    claimed_during_lookup = []

    async def _issues(_repo):
        claimed_during_lookup.append("my-app" in bot._checkbox_watchers)
        return []

    monkeypatch.setattr(bot.github, "open_release_issues", _issues)
    monkeypatch.setattr(bot.asyncio, "create_task", lambda _coro: None)

    await bot._cmd_wait_for_checkboxes(
        repos, slack.ack, slack.respond, _command("my-app"), {}
    )

    assert claimed_during_lookup == [True]


async def test_watch_checkboxes_leaves_the_ready_message_to_the_poller(
    repos, monkeypatch
):
    """The watcher must not post its own ready notification.

    `_notify_ready_to_promote` already posts one *with the promote button*,
    deduped by label. A second announcement here would put two ready messages
    in the channel, one of them without the button.
    """
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": "- [x] **A** (#1) by alice@example.com\n",
                    "labels": ["release"],
                }
            ]
        ),
    )
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._watch_checkboxes(app, "my-app", repos["my-app"], "C123")

    app.client.chat_postMessage.assert_not_called()
    assert "my-app" not in bot._checkbox_watchers


async def test_watch_checkboxes_sleeps_before_retrying_a_failed_refresh(
    repos, monkeypatch
):
    """Every loop path must pace itself against the GitHub API."""
    issue = {
        "number": 1,
        "title": "Release my-app",
        "url": "https://github.com/mitodl/my-app/issues/1",
        "body": "- [ ] **A** (#1) by alice@example.com\n",
        "labels": ["release"],
    }
    done = {"body": "- [x] **A** (#1) by alice@example.com\n"}
    calls = {"n": 0}

    async def _issues(_repo):
        calls["n"] += 1
        if calls["n"] == 2:  # the refresh
            msg = "GitHub is down"
            raise RuntimeError(msg)
        if calls["n"] >= 3:
            return [{**issue, **done}]
        return [issue]

    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot.github, "open_release_issues", _issues)
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep)
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._watch_checkboxes(app, "my-app", repos["my-app"], "C123")

    # One sleep for the normal cadence, one after the failed refresh.
    assert sleeps == [bot._CHECKBOX_POLL_SECONDS, bot._CHECKBOX_POLL_SECONDS]


async def test_watch_checkboxes_thanks_people_by_mention(repos, monkeypatch):
    """The thank-you is also the signal that a box was seen; it must notify."""
    issue = {
        "number": 1,
        "title": "Release my-app",
        "url": "https://github.com/mitodl/my-app/issues/1",
        "body": (
            "- [ ] **A** (#1) by alice@example.com\n"
            "- [ ] **B** (#2) by bob@example.com\n"
        ),
        "labels": ["release"],
    }
    alice_done = (
        "- [x] **A** (#1) by alice@example.com\n- [ ] **B** (#2) by bob@example.com\n"
    )
    refreshed = {**issue, "body": alice_done}
    done = {**issue, "body": alice_done.replace("- [ ]", "- [x]")}
    bodies = [issue, refreshed, done]

    async def _issues(_repo):
        return [bodies.pop(0)] if bodies else [done]

    monkeypatch.setattr(bot.github, "open_release_issues", _issues)
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    app = MagicMock()
    app.client = _slack_client({"alice@example.com": "UALICE"})

    await bot._watch_checkboxes(app, "my-app", repos["my-app"], "C123")

    posted = app.client.chat_postMessage.call_args_list[0].kwargs["text"]
    assert "<@UALICE>" in posted
    assert "alice@example.com" not in posted


# ---------------------------------------------------------------------------
# ready-to-promote notification
# ---------------------------------------------------------------------------


async def test_release_records_who_asked_for_it(repos, slack, monkeypatch):
    """The requester is who the ready-to-promote message has to ping."""
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))
    monkeypatch.setattr(bot.concourse, "check_resource", AsyncMock())
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(return_value="http://build/1")
    )

    await bot._cmd_release(
        repos, slack.ack, slack.respond, _command("my-app"), {"user_id": "UDANA"}
    )

    assert bot._release_requesters["my-app"] == "UDANA"


async def test_ready_to_promote_pings_the_release_requester(repos, monkeypatch):
    """Posting "ready to promote" addressed to nobody chases nobody.

    Doof pinged the release manager here; the closest thing this bot knows is
    whoever ran `/doof release`.
    """
    bot._release_requesters["my-app"] = "UDANA"
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": (
                        "## Release 2026.9.1.1\n\n- [x] **A** (#1) by alice@example.com"
                    ),
                    "labels": ["release"],
                }
            ]
        ),
    )
    monkeypatch.setattr(bot.github, "add_issue_label", AsyncMock())
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._notify_ready_to_promote(app, repos)

    blocks = app.client.chat_postMessage.call_args.kwargs["blocks"]
    assert "<@UDANA>" in blocks[0]["text"]["text"]


async def test_ready_to_promote_omits_the_ping_when_nobody_is_recorded(
    repos, monkeypatch
):
    """A bot restart loses the requester; the notification still has to go out."""
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": (
                        "## Release 2026.9.1.1\n\n- [x] **A** (#1) by alice@example.com"
                    ),
                    "labels": ["release"],
                }
            ]
        ),
    )
    monkeypatch.setattr(bot.github, "add_issue_label", AsyncMock())
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._notify_ready_to_promote(app, repos)

    blocks = app.client.chat_postMessage.call_args.kwargs["blocks"]
    assert "cc" not in blocks[0]["text"]["text"]
    assert blocks[1]["elements"][0]["value"] == "my-app:2026.9.1.1"


async def test_promote_clears_the_recorded_requester(repos, slack, monkeypatch):
    """A stale requester would ping the wrong person on the next release."""
    bot._release_requesters["my-app"] = "UDANA"
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": "## Release 2026.9.1.1\n",
                    "labels": ["release"],
                }
            ]
        ),
    )
    monkeypatch.setattr(bot.github, "close_release_issue", AsyncMock())
    monkeypatch.setattr(bot.concourse, "check_resource", AsyncMock())

    await bot._cmd_promote(
        repos, slack.ack, slack.respond, _command("my-app"), {"user_id": "UOTHER"}
    )

    assert "my-app" not in bot._release_requesters


def _ready_issue():
    return {
        "number": 1,
        "title": "Release my-app",
        "url": "https://github.com/mitodl/my-app/issues/1",
        "body": "## Release 2026.9.1.1\n\n- [x] **A** (#1) by alice@example.com",
        "labels": ["release"],
    }


async def test_ready_to_promote_stops_pinging_after_the_first_post(repos, monkeypatch):
    """A failed label write leaves the issue eligible on every 120s poll.

    The duplicate message is a known nuisance; repeating the @-mention with it
    would turn that into a notification every two minutes for the same person.
    """
    bot._release_requesters["my-app"] = "UDANA"
    monkeypatch.setattr(
        bot.github, "open_release_issues", AsyncMock(return_value=[_ready_issue()])
    )
    monkeypatch.setattr(
        bot.github,
        "add_issue_label",
        AsyncMock(side_effect=RuntimeError("GitHub is down")),
    )
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._notify_ready_to_promote(app, repos)
    await bot._notify_ready_to_promote(app, repos)

    first, second = app.client.chat_postMessage.call_args_list
    assert "<@UDANA>" in first.kwargs["blocks"][0]["text"]["text"]
    assert "<@UDANA>" not in second.kwargs["blocks"][0]["text"]["text"]


async def test_a_failed_post_keeps_the_requester_for_the_next_poll(repos, monkeypatch):
    """Dropping the requester on a failed post would lose the ping entirely."""
    bot._release_requesters["my-app"] = "UDANA"
    monkeypatch.setattr(
        bot.github, "open_release_issues", AsyncMock(return_value=[_ready_issue()])
    )
    monkeypatch.setattr(bot.github, "add_issue_label", AsyncMock())
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock(side_effect=RuntimeError("Slack is down"))

    await bot._notify_ready_to_promote(app, repos)

    assert bot._release_requesters["my-app"] == "UDANA"


# ---------------------------------------------------------------------------
# Deploy milestone announcements
# ---------------------------------------------------------------------------


def _deployment(deployment_id: int, version: str, environment: str) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "version": version,
        "sha": "abc123",
        "environment": environment,
        "deployed_at": datetime.now(tz=UTC),
        "url": "",
    }


@pytest.fixture
def slack_app():
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()
    return app


@pytest.fixture
def _no_release_issue(monkeypatch):
    monkeypatch.setattr(
        bot.github, "release_issue_for_version", AsyncMock(return_value=None)
    )


def _deployments_by_environment(mapping: dict[str, dict[str, Any] | None]):
    async def _lookup(_repo, environment):
        return mapping.get(environment)

    return _lookup


@pytest.mark.usefixtures("_no_release_issue")
async def test_first_poll_seeds_instead_of_announcing(repos, slack_app, monkeypatch):
    """A restart must not replay whatever is already deployed into the channel.

    The watcher reports transitions, so its first observation of an app is a
    baseline, not news -- otherwise every bot deploy would re-announce every
    app's current RC and production release.
    """
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {
                bot.github.RC_ENVIRONMENT: _deployment(1, "2026.9.2.1", "RC"),
                bot.github.PRODUCTION_ENVIRONMENT: _deployment(
                    2, "2026.9.1.1", "Production"
                ),
            }
        ),
    )
    state = bot.ReleaseProgressState()

    await bot._announce_deployments(slack_app, repos, state)

    slack_app.client.chat_postMessage.assert_not_called()
    assert state.last_version[("my-app", "RC")] == "2026.9.2.1"
    assert state.last_version[("my-app", "Production")] == "2026.9.1.1"


async def test_new_rc_deployment_announces_version_and_release_issue(
    repos, slack_app, monkeypatch
):
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {bot.github.RC_ENVIRONMENT: _deployment(7, "2026.9.2.1", "RC")}
        ),
    )
    monkeypatch.setattr(
        bot.github,
        "release_issue_for_version",
        AsyncMock(
            return_value={
                "number": 12,
                "url": "https://github.com/mitodl/my-app/issues/12",
                "title": "Release my-app 2026.9.2.1",
            }
        ),
    )
    state = bot.ReleaseProgressState(last_version={("my-app", "RC"): "2026.9.1.1"})

    await bot._announce_deployments(slack_app, repos, state)

    text = slack_app.client.chat_postMessage.call_args.kwargs["text"]
    assert "2026.9.2.1" in text
    assert "RC" in text
    # The link is the whole point: it is what makes "what is in this release"
    # one click away, which is what the kubewatch notifications lack.
    assert "https://github.com/mitodl/my-app/issues/12" in text


@pytest.mark.usefixtures("_no_release_issue")
async def test_production_deployment_announces_separately(
    repos, slack_app, monkeypatch
):
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {
                bot.github.PRODUCTION_ENVIRONMENT: _deployment(
                    9, "2026.9.2.1", "Production"
                )
            }
        ),
    )
    state = bot.ReleaseProgressState(
        last_version={("my-app", "Production"): "2026.9.1.1"}
    )

    await bot._announce_deployments(slack_app, repos, state)

    text = slack_app.client.chat_postMessage.call_args.kwargs["text"]
    assert "production" in text.lower()
    assert "2026.9.2.1" in text


@pytest.mark.usefixtures("_no_release_issue")
async def test_unchanged_deployment_is_not_re_announced(repos, slack_app, monkeypatch):
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {bot.github.RC_ENVIRONMENT: _deployment(7, "2026.9.2.1", "RC")}
        ),
    )
    state = bot.ReleaseProgressState()

    await bot._announce_deployments(slack_app, repos, state)
    await bot._announce_deployments(slack_app, repos, state)
    await bot._announce_deployments(slack_app, repos, state)

    slack_app.client.chat_postMessage.assert_not_called()


@pytest.mark.usefixtures("_no_release_issue")
async def test_an_infra_only_redeploy_of_the_same_version_is_not_announced(
    repos, slack_app, monkeypatch
):
    """A new Deployment id for the release already on RC is not a release.

    Regression: the QA job re-runs on any merge under the ol-infrastructure
    paths it watches (src/ol_infrastructure/lib among them) and records a fresh
    RC Deployment each time, with the same version. Keying on the id announced
    ol-analytics-api 2026.10.1.1 as "deployed to RC" after an unrelated
    labels change, six days after it actually was.
    """
    current = {"deployment": _deployment(7, "2026.10.1.1", "RC")}

    async def _lookup(_repo, environment):
        if environment != bot.github.RC_ENVIRONMENT:
            return None
        return current["deployment"]

    monkeypatch.setattr(bot.github, "latest_successful_deployment", _lookup)
    state = bot.ReleaseProgressState()
    await bot._announce_deployments(slack_app, repos, state)

    current["deployment"] = _deployment(8, "2026.10.1.1", "RC")
    await bot._announce_deployments(slack_app, repos, state)
    slack_app.client.chat_postMessage.assert_not_called()

    current["deployment"] = _deployment(9, "2026.10.7.1", "RC")
    await bot._announce_deployments(slack_app, repos, state)
    slack_app.client.chat_postMessage.assert_called_once()
    assert "2026.10.7.1" in slack_app.client.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.usefixtures("_no_release_issue")
async def test_a_brand_new_apps_first_ever_deployment_is_announced(
    repos, slack_app, monkeypatch
):
    """A never-deployed app's first real deployment is news, not a restart-seed.

    Regression: collapsing "never polled this (app, environment)" and "polled
    it and found nothing yet" both looked like `.get(key) is None`, so the
    first real deployment for a brand-new app was silently swallowed as if it
    were an already-known deployment observed right after a bot restart.
    """
    rc_calls = {"n": 0}

    async def _lookup(_repo, environment):
        # Production never has a deployment in this test; only RC's polls
        # progress, so the assertions below can inspect a single message.
        if environment != bot.github.RC_ENVIRONMENT:
            return None
        rc_calls["n"] += 1
        return None if rc_calls["n"] == 1 else _deployment(7, "2026.9.2.1", "RC")

    monkeypatch.setattr(bot.github, "latest_successful_deployment", _lookup)
    state = bot.ReleaseProgressState()

    await bot._announce_deployments(slack_app, repos, state)
    slack_app.client.chat_postMessage.assert_not_called()
    assert state.last_version[("my-app", "RC")] is None

    await bot._announce_deployments(slack_app, repos, state)

    slack_app.client.chat_postMessage.assert_called_once()
    text = slack_app.client.chat_postMessage.call_args.kwargs["text"]
    assert "2026.9.2.1" in text
    assert state.last_version[("my-app", "RC")] == "2026.9.2.1"


@pytest.mark.usefixtures("_no_release_issue")
async def test_a_failed_post_is_retried_on_the_next_poll(repos, slack_app, monkeypatch):
    """Recording a milestone as announced when the post failed loses it forever."""
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {bot.github.RC_ENVIRONMENT: _deployment(7, "2026.9.2.1", "RC")}
        ),
    )
    slack_app.client.chat_postMessage = AsyncMock(
        side_effect=RuntimeError("slack down")
    )
    state = bot.ReleaseProgressState(last_version={("my-app", "RC"): "2026.9.1.1"})

    await bot._announce_deployments(slack_app, repos, state)

    assert state.last_version[("my-app", "RC")] == "2026.9.1.1"
    slack_app.client.chat_postMessage = AsyncMock()
    await bot._announce_deployments(slack_app, repos, state)
    slack_app.client.chat_postMessage.assert_called_once()


# ---------------------------------------------------------------------------
# Stuck-release reporting
# ---------------------------------------------------------------------------


def _in_flight(version: str, age: timedelta) -> dict[str, Any]:
    return {
        "version": version,
        "branch": f"releases/{version}",
        "url": f"https://github.com/mitodl/my-app/tree/releases/{version}",
        "cut_at": datetime.now(tz=UTC) - age,
    }


async def test_a_young_release_is_not_reported_as_stuck(repos, slack_app, monkeypatch):
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.2.1", timedelta(hours=2))),
    )

    await bot._nag_stuck_releases(slack_app, repos, bot.ReleaseProgressState())

    slack_app.client.chat_postMessage.assert_not_called()


async def test_a_release_deployed_to_production_but_unfinished_is_called_out(
    repos, slack_app, monkeypatch
):
    """The ol-analytics-api failure: shipped, branch never merged, nothing red.

    This has to read differently from "waiting to be promoted" -- the branch
    outliving its production deploy is what freezes the calver counter, so the
    next release collides with this version.
    """
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.8.3.1", timedelta(days=30))),
    )
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        _deployments_by_environment(
            {
                bot.github.RC_ENVIRONMENT: _deployment(1, "2026.8.3.1", "RC"),
                bot.github.PRODUCTION_ENVIRONMENT: _deployment(
                    2, "2026.8.3.1", "Production"
                ),
            }
        ),
    )
    state = bot.ReleaseProgressState()

    await bot._nag_stuck_releases(slack_app, repos, state)

    text = slack_app.client.chat_postMessage.call_args.kwargs["text"]
    assert "2026.8.3.1" in text
    assert "30d" in text
    assert "never finished" in text
    assert "releases/2026.8.3.1" in text
    assert state.nagged_at[("my-app", "2026.8.3.1")] is not None


async def test_a_release_that_never_reached_rc_says_so(repos, slack_app, monkeypatch):
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.1.1", timedelta(days=2))),
    )
    monkeypatch.setattr(
        bot.github, "latest_successful_deployment", _deployments_by_environment({})
    )

    await bot._nag_stuck_releases(slack_app, repos, bot.ReleaseProgressState())

    text = slack_app.client.chat_postMessage.call_args.kwargs["text"]
    assert "has not reached RC" in text


async def test_a_stuck_release_is_not_re_reported_every_poll(
    repos, slack_app, monkeypatch
):
    """Doof re-nagged every 24h, not every poll cycle."""
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.1.1", timedelta(days=2))),
    )
    monkeypatch.setattr(
        bot.github, "latest_successful_deployment", _deployments_by_environment({})
    )
    state = bot.ReleaseProgressState()

    await bot._nag_stuck_releases(slack_app, repos, state)
    await bot._nag_stuck_releases(slack_app, repos, state)
    await bot._nag_stuck_releases(slack_app, repos, state)

    slack_app.client.chat_postMessage.assert_called_once()


async def test_the_stuck_threshold_is_configurable(repos, slack_app, monkeypatch):
    monkeypatch.setenv("RELEASE_STUCK_AFTER_HOURS", "1")
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.2.1", timedelta(hours=2))),
    )
    monkeypatch.setattr(
        bot.github, "latest_successful_deployment", _deployments_by_environment({})
    )

    await bot._nag_stuck_releases(slack_app, repos, bot.ReleaseProgressState())

    slack_app.client.chat_postMessage.assert_called_once()


# ---------------------------------------------------------------------------
# channel resolution
# ---------------------------------------------------------------------------


async def test_proactive_posts_go_to_the_resolved_channel_id(repos, monkeypatch):
    """chat.postMessage rejects the configured name; it must get the id."""
    monkeypatch.setattr(
        bot.slack_channels, "resolve", AsyncMock(return_value="C0RESOLVED")
    )
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Release my-app",
                    "url": "https://github.com/mitodl/my-app/issues/1",
                    "body": (
                        "## Release 2026.9.1.1\n\n- [x] **A** (#1) by alice@example.com"
                    ),
                    "labels": ["release"],
                }
            ]
        ),
    )
    monkeypatch.setattr(bot.github, "add_issue_label", AsyncMock())
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    await bot._notify_ready_to_promote(app, repos)

    assert app.client.chat_postMessage.call_args.kwargs["channel"] == "C0RESOLVED"


async def test_startup_reports_channels_the_bot_cannot_reach(
    repos, monkeypatch, caplog
):
    """A channel the bot was never invited to must be visible at boot.

    Otherwise it surfaces only as a failed post inside a poll loop, which is
    how 137 channel_not_found errors went unnoticed.
    """
    monkeypatch.setattr(
        bot.slack_channels,
        "unresolvable",
        AsyncMock(return_value=["product-mit-learn"]),
    )
    app = MagicMock()

    with caplog.at_level("ERROR"):
        await bot._report_unreachable_channels(app, repos)

    assert "product-mit-learn" in caplog.text


async def test_startup_check_survives_a_slack_failure(repos, monkeypatch):
    """A validation that can kill startup is worse than no validation."""
    monkeypatch.setattr(
        bot.slack_channels,
        "unresolvable",
        AsyncMock(side_effect=RuntimeError("Slack is down")),
    )

    await bot._report_unreachable_channels(MagicMock(), repos)


async def test_release_refuses_an_app_still_on_the_legacy_pipeline(
    mixed_repos, slack, monkeypatch
):
    """`build-<app>-release-image` does not exist in the legacy pipeline.

    Triggering it 404s in Concourse with nothing in the message that explains
    why, so the refusal has to happen before the call.
    """
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(bot.concourse, "check_resource", AsyncMock())
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=_record_trigger(calls))
    )
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))

    await bot._cmd_release(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {}
    )

    assert calls == []
    assert "legacy" in slack.said
    assert "Doof" in slack.said


async def test_preview_refuses_a_legacy_app_rather_than_answering_wrongly(
    mixed_repos, slack, monkeypatch
):
    """A repo with no calver tag reads as never-released.

    `release_preview` would then report the whole commit history as the next
    release's contents -- a confidently wrong answer, which is worse than the
    404 the mutating commands get.
    """
    preview = AsyncMock()
    monkeypatch.setattr(bot.github, "release_preview", preview)

    await bot._cmd_preview(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {}
    )

    preview.assert_not_awaited()
    assert "legacy" in slack.said


async def test_promote_refuses_a_legacy_app(mixed_repos, slack, monkeypatch):
    """Closing a release issue is only half of promote; the gate is the rest."""
    close = AsyncMock()
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot.github, "close_release_issue", close)

    await bot._cmd_promote(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {"user_id": "U1"}
    )

    close.assert_not_awaited()
    assert "legacy" in slack.said


async def test_abandon_refuses_a_legacy_app(mixed_repos, slack, monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=_record_trigger(calls))
    )

    await bot._cmd_abandon(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {}
    )

    assert calls == []
    assert "legacy" in slack.said


async def test_promote_button_refuses_a_legacy_app(mixed_repos, monkeypatch):
    """The button carries an app name from a message, so it is not trusted."""
    close = AsyncMock()
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot.github, "close_release_issue", close)
    say = AsyncMock()
    client = AsyncMock()

    await bot._handle_promote_button(
        mixed_repos, AsyncMock(), _promote_body("legacy-app"), say, client
    )

    close.assert_not_awaited()
    assert "legacy" in str(say.call_args.args[0])
    # The payload carries channel/ts/blocks, so this would catch an edit
    # added to the legacy path -- the message must stay untouched.
    client.chat_update.assert_not_awaited()


async def test_wait_for_checkboxes_refuses_a_legacy_app(
    mixed_repos, slack, monkeypatch
):
    """No release issue is ever created for a legacy app, so nothing to watch."""
    monkeypatch.setattr(bot, "_slack_app", MagicMock())
    issues = AsyncMock(return_value=[])
    monkeypatch.setattr(bot.github, "open_release_issues", issues)

    await bot._cmd_wait_for_checkboxes(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {}
    )

    issues.assert_not_awaited()
    assert "legacy-app" not in bot._checkbox_watchers
    assert "legacy" in slack.said


async def test_release_status_names_the_apps_it_left_out(
    mixed_repos, slack, monkeypatch
):
    """A short list must not read as "everything is quiet" during the rollout."""
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))

    await bot._cmd_release_status(
        mixed_repos, slack.ack, slack.respond, _command(""), {}
    )

    said = slack.said
    assert "my-app" in said
    assert "legacy-app" in said
    assert "legacy pipeline" in said


async def test_release_status_queries_only_migrated_apps(
    mixed_repos, slack, monkeypatch
):
    queried: list[str] = []

    async def _issues(repo_slug):
        queried.append(repo_slug)
        return []

    monkeypatch.setattr(bot.github, "open_release_issues", _issues)
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))

    await bot._cmd_release_status(
        mixed_repos, slack.ack, slack.respond, _command(""), {}
    )

    assert queried == ["mitodl/my-app"]


async def test_main_polls_only_migrated_apps(mixed_repos, monkeypatch):
    """Polling a legacy repo every 120s is a guaranteed miss on rate limit.

    Driven through `main()` rather than through the filter helper, because
    the helper being correct proves nothing if `main()` hands either loop the
    unfiltered mapping -- which is the wiring that would actually regress.
    """
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    app = MagicMock()
    monkeypatch.setattr(bot, "create_app", lambda: (app, mixed_repos))
    monkeypatch.setattr(bot, "_report_unreachable_channels", AsyncMock())

    polled: list[list[str]] = []

    async def _loop(_app, repos):
        polled.append(list(repos))

    monkeypatch.setattr(bot, "_poll_ready_to_promote_loop", _loop)
    monkeypatch.setattr(bot, "_poll_release_progress_loop", _loop)
    handler = MagicMock()
    handler.start_async = AsyncMock()
    monkeypatch.setattr(bot, "AsyncSocketModeHandler", MagicMock(return_value=handler))

    await bot.main()
    # main() starts the loops with create_task and then blocks on the socket
    # handler; yield once so both scheduled tasks actually run.
    await asyncio.sleep(0)

    assert polled == [["my-app"], ["my-app"]]
    handler.start_async.assert_awaited_once()


async def test_startup_channel_check_still_covers_legacy_apps(mixed_repos, monkeypatch):
    """Narrowing the pollers must not narrow the boot-time channel check.

    An unreachable channel is worth finding before an app is migrated, not on
    its first release.
    """
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    checked = AsyncMock()
    monkeypatch.setattr(bot, "create_app", lambda: (MagicMock(), mixed_repos))
    monkeypatch.setattr(bot, "_report_unreachable_channels", checked)

    async def _loop(_app, _repos):
        return

    monkeypatch.setattr(bot, "_poll_ready_to_promote_loop", _loop)
    monkeypatch.setattr(bot, "_poll_release_progress_loop", _loop)
    handler = MagicMock()
    handler.start_async = AsyncMock()
    monkeypatch.setattr(bot, "AsyncSocketModeHandler", MagicMock(return_value=handler))

    await bot.main()
    await asyncio.sleep(0)

    checked.assert_awaited_once()
    assert list(checked.await_args_list[0].args[1]) == ["my-app", "legacy-app"]


# ---------------------------------------------------------------------------
# /doof publish
# ---------------------------------------------------------------------------


@pytest.fixture
def _libraries(monkeypatch):
    """Install a monorepo and a single-artifact library, both in team `main`."""
    monkeypatch.setattr(
        bot,
        "_libraries",
        {
            "ol-django": bot_config.LibraryConfig(
                pipeline="publish-ol-django-pypi",
                team="main",
                package_job_prefix="build-",
                github_repo="mitodl/ol-django",
            ),
            "mit-learn-api-client": bot_config.LibraryConfig(
                pipeline="mit-learn-api-client",
                team="main",
                publish_job="publish",
                registry="npm",
            ),
        },
    )
    monkeypatch.setattr(
        bot,
        "_unpublishable_libraries",
        {"edx-api-client": "no publish pipeline yet -- still goes through Doof."},
    )
    monkeypatch.setattr(
        bot.concourse, "pipeline_is_paused", AsyncMock(return_value=False)
    )


def _record_publish(calls: list[tuple[str, str, str]]):
    """Record a (pipeline, job, team) trigger and return the reported build URL."""

    def _trigger(pipeline, job, team):
        calls.append((pipeline, job, team))
        return "http://build/1"

    return _trigger


@pytest.mark.usefixtures("_libraries")
async def test_publish_triggers_the_registered_job_on_the_registered_team(
    repos, slack, monkeypatch
):
    """The whole bug in one assertion.

    The old handler looked the name up in `repos` (apps), triggered a job
    literally named `publish` in `<name>-pipeline`, and used the bot's ambient
    `infrastructure` team. All three were wrong for every library.
    """
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=_record_publish(calls))
    )

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("mit-learn-api-client"), {}
    )

    assert calls == [("mit-learn-api-client", "publish", "main")]
    assert "npm" in slack.said
    assert "http://build/1" in slack.said


@pytest.mark.usefixtures("_libraries")
async def test_publish_resolves_a_monorepo_package_to_its_job(
    repos, slack, monkeypatch
):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bot.concourse, "list_jobs", AsyncMock(return_value=["build-mail", "build-scim"])
    )
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=_record_publish(calls))
    )

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("ol-django scim"), {}
    )

    assert calls == [("publish-ol-django-pypi", "build-scim", "main")]


@pytest.mark.usefixtures("_libraries")
async def test_publish_lists_a_monorepos_packages_when_none_is_named(
    repos, slack, monkeypatch
):
    """Read off the live pipeline: a static list goes stale on the next package."""
    monkeypatch.setattr(
        bot.concourse, "list_jobs", AsyncMock(return_value=["build-mail", "build-scim"])
    )
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(repos, slack.ack, slack.respond, _command("ol-django"), {})

    assert "mail" in slack.said
    assert "scim" in slack.said
    trigger.assert_not_awaited()


@pytest.mark.usefixtures("_libraries")
async def test_publish_rejects_a_package_the_pipeline_does_not_define(
    repos, slack, monkeypatch
):
    monkeypatch.setattr(
        bot.concourse, "list_jobs", AsyncMock(return_value=["build-mail"])
    )
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("ol-django nonesuch"), {}
    )

    assert "nonesuch" in slack.said
    trigger.assert_not_awaited()


@pytest.mark.usefixtures("_libraries")
async def test_publish_refuses_a_paused_pipeline_rather_than_reporting_a_dead_build(
    repos, slack, monkeypatch
):
    """A build in a paused pipeline is created and then never scheduled.

    Reporting its URL reads as success while the build sits at "pending"
    forever. publish-ol-django-pypi is paused today, so this is the first
    thing a real publish of it would hit.
    """
    monkeypatch.setattr(
        bot.concourse, "list_jobs", AsyncMock(return_value=["build-mail"])
    )
    monkeypatch.setattr(
        bot.concourse, "pipeline_is_paused", AsyncMock(return_value=True)
    )
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("ol-django mail"), {}
    )

    assert "paused" in slack.said
    trigger.assert_not_awaited()


@pytest.mark.usefixtures("_libraries")
async def test_publish_still_triggers_when_the_paused_check_fails(
    repos, slack, monkeypatch
):
    """The paused check is advisory; failing it must not block a publish."""
    monkeypatch.setattr(
        bot.concourse,
        "pipeline_is_paused",
        AsyncMock(side_effect=RuntimeError("Concourse is down")),
    )
    trigger = AsyncMock(return_value="http://build/1")
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("mit-learn-api-client"), {}
    )

    trigger.assert_awaited_once()


@pytest.mark.usefixtures("_libraries")
async def test_publish_explains_a_library_that_has_no_pipeline(
    repos, slack, monkeypatch
):
    """Doof can publish these. Calling them "unknown" would read as a typo."""
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(
        repos, slack.ack, slack.respond, _command("edx-api-client"), {}
    )

    assert "still goes through Doof" in slack.said
    trigger.assert_not_awaited()


@pytest.mark.usefixtures("_libraries")
async def test_publish_points_an_app_name_at_the_release_command(
    repos, slack, monkeypatch
):
    """`my-app` is in the app registry; publish is the wrong verb for it."""
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(repos, slack.ack, slack.respond, _command("my-app"), {})

    assert "/doof release my-app" in slack.said
    trigger.assert_not_awaited()


@pytest.mark.usefixtures("_libraries")
async def test_publish_with_no_argument_lists_the_libraries(repos, slack, monkeypatch):
    trigger = AsyncMock()
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_publish(repos, slack.ack, slack.respond, _command(""), {})

    assert "ol-django" in slack.said
    assert "mit-learn-api-client" in slack.said
    trigger.assert_not_awaited()


# /doof hotfix, and what a pending request does to release and abandon
# ---------------------------------------------------------------------------

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


@pytest.fixture
def hotfix_api(monkeypatch):
    """Stub every GitHub and Concourse call /doof hotfix makes, in call order."""
    api = MagicMock()
    api.calls = []
    api.resolve_commit = AsyncMock(return_value=_SHA)
    api.in_flight_release = AsyncMock(return_value=None)
    api.pending_hotfix_requests = AsyncMock(return_value=[])
    api.request_hotfix = AsyncMock(
        side_effect=lambda repo, sha: api.calls.append(("request", repo, sha))
    )
    api.check_resource = AsyncMock(
        side_effect=lambda p, r: api.calls.append(("check", p, r))
    )
    api.trigger_job = AsyncMock(side_effect=_record_trigger(api.calls))
    for name in (
        "resolve_commit",
        "in_flight_release",
        "pending_hotfix_requests",
        "request_hotfix",
    ):
        monkeypatch.setattr(bot.github, name, getattr(api, name))
    for name in ("check_resource", "trigger_job"):
        monkeypatch.setattr(bot.concourse, name, getattr(api, name))
    return api


async def test_hotfix_requests_then_checks_then_triggers(repos, slack, hotfix_api):
    """The request has to exist before the check, or check offers a release."""
    await bot._cmd_hotfix(
        repos,
        slack.ack,
        slack.respond,
        _command("my-app aaaaaaa"),
        {"user_id": "UDANA"},
    )

    hotfix_api.resolve_commit.assert_awaited_once_with("mitodl/my-app", "aaaaaaa")
    assert hotfix_api.calls == [
        ("request", "mitodl/my-app", _SHA),
        ("check", "my-app-pipeline", "my-app-release"),
        ("trigger", "my-app-pipeline", "build-my-app-release-image"),
    ]
    assert "Hotfix of `aaaaaaa` triggered" in slack.said
    assert bot._release_requesters["my-app"] == "UDANA"


async def test_hotfix_rejects_something_that_is_not_a_sha(repos, slack, hotfix_api):
    await bot._cmd_hotfix(repos, slack.ack, slack.respond, _command("my-app main"), {})

    assert slack.said.startswith("Usage:")
    assert hotfix_api.calls == []


async def test_hotfix_refuses_while_a_release_is_in_flight(repos, slack, hotfix_api):
    """The release resource refuses too, but only once a build is running."""
    hotfix_api.in_flight_release.return_value = _in_flight(
        "2026.9.9.1", timedelta(days=1)
    )

    await bot._cmd_hotfix(
        repos, slack.ack, slack.respond, _command(f"my-app {_SHA}"), {}
    )

    assert "is in flight" in slack.said
    assert hotfix_api.calls == []


async def test_hotfix_refuses_while_another_hotfix_is_pending(repos, slack, hotfix_api):
    hotfix_api.pending_hotfix_requests.return_value = [_OTHER_SHA]

    await bot._cmd_hotfix(
        repos, slack.ack, slack.respond, _command(f"my-app {_SHA}"), {}
    )

    assert "`bbbbbbb` is already pending" in slack.said
    assert hotfix_api.calls == []


async def test_hotfix_reuses_its_own_pending_request(repos, slack, hotfix_api):
    """Re-running after a failed trigger must not trip over its own tag."""
    hotfix_api.pending_hotfix_requests.return_value = [_SHA]

    await bot._cmd_hotfix(
        repos, slack.ack, slack.respond, _command(f"my-app {_SHA}"), {}
    )

    assert hotfix_api.calls == [
        ("check", "my-app-pipeline", "my-app-release"),
        ("trigger", "my-app-pipeline", "build-my-app-release-image"),
    ]


async def test_hotfix_says_how_to_retry_when_the_trigger_fails(
    repos, slack, hotfix_api
):
    hotfix_api.trigger_job.side_effect = RuntimeError("boom")

    await bot._cmd_hotfix(
        repos, slack.ack, slack.respond, _command(f"my-app {_SHA}"), {}
    )

    assert "requested but not started" in slack.said


async def test_release_refuses_while_a_hotfix_is_pending(repos, slack, monkeypatch):
    """The build would cut the pending hotfix, not the release asked for."""
    trigger = AsyncMock()
    monkeypatch.setattr(
        bot.github, "pending_hotfix_requests", AsyncMock(return_value=[_SHA])
    )
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_release(repos, slack.ack, slack.respond, _command("my-app"), {})

    trigger.assert_not_called()
    assert "would cut that hotfix instead" in slack.said


async def test_abandon_with_nothing_in_flight_only_cancels_requests(
    repos, slack, monkeypatch
):
    """With nothing in flight, the abandon job can delete production's tag."""
    trigger = AsyncMock()
    monkeypatch.setattr(
        bot.github, "cancel_hotfix_requests", AsyncMock(return_value=[_SHA])
    )
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))
    monkeypatch.setattr(bot.concourse, "trigger_job", trigger)

    await bot._cmd_abandon(repos, slack.ack, slack.respond, _command("my-app"), {})

    trigger.assert_not_called()
    assert "Cancelled the pending hotfix of `aaaaaaa`" in slack.said
    assert "nothing to abandon" in slack.said


async def test_abandon_triggers_the_job_for_an_in_flight_release(
    repos, slack, monkeypatch
):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        bot.github, "cancel_hotfix_requests", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.9.1", timedelta(days=1))),
    )
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=_record_trigger(calls))
    )

    await bot._cmd_abandon(repos, slack.ack, slack.respond, _command("my-app"), {})

    assert calls == [("trigger", "my-app-pipeline", "abandon-my-app-release")]
    said = slack.said
    # Not "Abandoning <version>": the job deletes whatever version the
    # release resource's last `check` recorded, which can diverge from what
    # in_flight_release reads fresh from GitHub branches.
    assert "abandon triggered" in said
    assert "last seen in flight" in said
    assert "2026.9.9.1" in said


async def test_abandon_reports_cancelled_hotfixes_when_the_trigger_fails(
    repos, slack, monkeypatch
):
    """A failed trigger must not hide that hotfix requests were already cancelled."""
    monkeypatch.setattr(
        bot.github, "cancel_hotfix_requests", AsyncMock(return_value=[_SHA])
    )
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(return_value=_in_flight("2026.9.9.1", timedelta(days=1))),
    )
    monkeypatch.setattr(
        bot.concourse, "trigger_job", AsyncMock(side_effect=RuntimeError("boom"))
    )

    await bot._cmd_abandon(repos, slack.ack, slack.respond, _command("my-app"), {})

    said = slack.said
    assert "Cancelled the pending hotfix of `aaaaaaa`" in said
    assert "Failed to trigger release abandon" in said


async def test_abandon_cancels_nothing_when_it_cannot_read_release_state(
    repos, slack, monkeypatch
):
    """Deleting requests and then reporting "nothing abandoned" would hide it."""
    cancel = AsyncMock()
    monkeypatch.setattr(bot.github, "cancel_hotfix_requests", cancel)
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(side_effect=RuntimeError("github down")),
    )

    await bot._cmd_abandon(repos, slack.ack, slack.respond, _command("my-app"), {})

    cancel.assert_not_awaited()
    assert "Nothing abandoned" in slack.said


async def test_concurrent_hotfixes_for_one_app_cannot_both_be_requested(
    repos, slack, hotfix_api
):
    """Without the per-app lock both read "nothing pending" before either writes."""
    pending: list[str] = []

    async def _request(_repo, sha):
        await asyncio.sleep(0)  # where a racing command could slip in
        pending.append(sha)

    hotfix_api.resolve_commit.side_effect = lambda _repo, ref: ref[0] * 40
    hotfix_api.pending_hotfix_requests.side_effect = lambda _repo: list(pending)
    hotfix_api.request_hotfix.side_effect = _request

    await asyncio.gather(
        bot._cmd_hotfix(
            repos, slack.ack, slack.respond, _command("my-app aaaaaaa"), {}
        ),
        bot._cmd_hotfix(
            repos, slack.ack, slack.respond, _command("my-app bbbbbbb"), {}
        ),
    )

    assert pending == ["a" * 40]
    assert "`aaaaaaa` is already pending" in slack.said


# ---------------------------------------------------------------------------
# the promote button's own message
# ---------------------------------------------------------------------------


_PROMOTE_TS = "1700000000.000100"


@pytest.fixture(autouse=True)
def _clear_promoting():
    """Reset the claim set; one leaking between tests makes the next click a no-op."""
    bot._promoting.clear()
    yield
    bot._promoting.clear()


def _promote_body(app_name="my-app", version="2026.9.5.1", user="U1"):
    """Build a block_actions payload shaped like the one Slack sends for the button."""
    return {
        "actions": [{"value": f"{app_name}:{version}"}],
        "user": {"id": user},
        "channel": {"id": "C123"},
        "message": {
            "ts": _PROMOTE_TS,
            "blocks": bot._ready_to_promote_blocks(app_name, version, "http://issue/1"),
        },
    }


@pytest.fixture
def promote_api(monkeypatch):
    """Stub the GitHub and Concourse calls behind a successful promotion."""
    api = MagicMock()
    api.open_release_issues = AsyncMock(
        return_value=[
            {"number": 7, "body": "## Release 2026.9.5.1", "url": "http://issue/1"}
        ]
    )
    api.close_release_issue = AsyncMock()
    api.check_resource = AsyncMock()
    monkeypatch.setattr(bot.github, "open_release_issues", api.open_release_issues)
    monkeypatch.setattr(bot.github, "close_release_issue", api.close_release_issue)
    monkeypatch.setattr(bot.concourse, "check_resource", api.check_resource)
    return api


@pytest.mark.usefixtures("promote_api")
async def test_promote_button_is_removed_once_the_promotion_lands(repos):
    """The clicked message loses its button so it cannot be clicked again."""
    client = AsyncMock()
    say = AsyncMock()

    await bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client)

    edit = client.chat_update.call_args.kwargs
    assert edit["channel"] == "C123"
    assert edit["ts"] == _PROMOTE_TS
    # Everything but the button survives (the release summary and issue
    # link), with the status appended -- not merely "no actions block",
    # which an empty blocks list would also satisfy.
    kept = [
        b for b in _promote_body()["message"]["blocks"] if b.get("type") != "actions"
    ]
    assert edit["blocks"][:-1] == kept
    assert edit["blocks"][-1]["type"] == "context"
    assert "<@U1> promoted this to Production" in edit["text"]
    # The edit is silent, so the channel post is what actually notifies.
    assert "promoted `my-app`" in str(say.call_args.args[0])


async def test_promote_button_is_left_alone_when_the_promotion_fails(
    repos, promote_api
):
    """A retryable failure keeps the message -- and its button -- intact.

    A crash mid-callback freezes this message forever (nothing stores its ts),
    so the button may only disappear on a terminal outcome; anything else must
    leave it clickable for the retry. The claim is released for the same
    reason.
    """
    promote_api.close_release_issue.side_effect = RuntimeError("GitHub is down")
    client = AsyncMock()
    say = AsyncMock()

    await bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client)

    client.chat_update.assert_not_awaited()
    assert "Failed to promote" in str(say.call_args.args[0])
    assert bot._promoting == set()


async def test_promote_button_resolves_a_message_stranded_by_a_restart(
    repos, promote_api
):
    """An already-closed issue settles the stale button instead of erroring.

    This is the repair path for a bot that died after closing the issue and
    before editing the message: the next click removes the button and settles
    the message instead of reporting a lookup failure.
    """
    promote_api.open_release_issues.return_value = []
    client = AsyncMock()
    say = AsyncMock()

    await bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client)

    edit = client.chat_update.call_args.kwargs
    assert [b for b in edit["blocks"] if b.get("type") == "actions"] == []
    assert "No open release issue remains" in edit["text"]
    promote_api.close_release_issue.assert_not_awaited()
    # The channel post is the only notifying message, so it must be the
    # neutral explanation, not the old lookup-failure warning.
    posted = str(say.call_args.args[0])
    assert "Could not find" not in posted
    assert "has no open release issue" in posted


async def test_a_second_click_cannot_promote_the_same_release_twice(repos, promote_api):
    """Two concurrent clicks must not both close the issue."""
    closes = []

    async def _close(_repo, number, comment):  # noqa: ARG001
        await asyncio.sleep(0)  # where the second click would slip in
        closes.append(number)

    promote_api.close_release_issue.side_effect = _close
    client = AsyncMock()
    say = AsyncMock()

    await asyncio.gather(
        bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client),
        bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client),
    )

    assert closes == [7]
    assert any(
        "already being promoted" in str(call.args[0])
        for call in say.call_args_list
        if call.args
    )


async def test_a_broken_message_edit_does_not_stop_the_promotion(repos, promote_api):
    """Slack refusing the edit is cosmetic; the deploy must still go out."""
    client = AsyncMock()
    client.chat_update.side_effect = RuntimeError("Slack is down")
    say = AsyncMock()

    await bot._handle_promote_button(repos, AsyncMock(), _promote_body(), say, client)

    promote_api.close_release_issue.assert_awaited_once()
    promote_api.check_resource.assert_awaited_once_with(
        "my-app-pipeline", "my-app-release-gate"
    )
    assert "promoted `my-app`" in str(say.call_args.args[0])


# ---------------------------------------------------------------------------
# /doof help, /doof version, /doof uptime
# ---------------------------------------------------------------------------


async def test_help_is_a_subcommand_not_only_a_parse_failure(repos, slack):
    await bot._cmd_doof(repos, slack.ack, slack.respond, _command("help"), {})

    said = slack.said
    for subcommand in ("release", "version", "uptime", "help"):
        assert f"/doof {subcommand}" in said
    # Every subcommand the dispatcher accepts is listed in the usage text.
    for subcommand in bot._SUBCOMMANDS:
        assert f"/doof {subcommand}" in bot._USAGE


def _live_deployment(version, sha, environment, url="", age=timedelta(hours=5)):
    return {
        "id": 1,
        "version": version,
        "sha": sha,
        "environment": environment,
        "deployed_at": datetime.now(tz=UTC) - age,
        "url": url,
    }


def _report(success=None, newer=None):
    return {"success": success, "newer": newer}


async def test_version_reports_production_and_rc(repos, slack, monkeypatch):
    reports = {
        "Production": _report(
            _live_deployment(
                "2026.10.8.1", "a" * 40, "Production", url="https://ci/builds/27"
            )
        ),
        "RC": _report(
            _live_deployment("2026.10.9.1", "b" * 40, "RC", age=timedelta(minutes=3))
        ),
    }

    async def _deployment_report(repo, environment):
        assert repo == "mitodl/my-app"
        return reports[environment]

    monkeypatch.setattr(bot.github, "deployment_report", _deployment_report)

    await bot._cmd_version(repos, slack.ack, slack.respond, _command("my-app"), {})

    said = slack.said
    assert (
        "• Production: <https://ci/builds/27|2026.10.8.1> · "
        f"<https://github.com/mitodl/my-app/commit/{'a' * 40}|aaaaaaa> · "
        "deployed 5h ago"
    ) in said
    # No build link recorded: the version is shown bare.
    assert "• RC: 2026.10.9.1 · " in said
    assert "deployed 3m ago" in said
    assert said.index("Production") < said.index("RC")
    assert "newer" not in said


async def test_version_names_a_newer_deployment_that_did_not_succeed(
    repos, slack, monkeypatch
):
    """A job that deployed and then failed leaves RC running something newer."""
    newer = {
        "version": "2026.10.9.1",
        "sha": "c" * 40,
        "created_at": datetime.now(tz=UTC) - timedelta(minutes=20),
        "state": "failure",
    }

    async def _deployment_report(_repo, environment):
        if environment == "RC":
            return _report(_live_deployment("2026.10.8.1", "a" * 40, "RC"), newer)
        return _report(newer=dict(newer, state="no status"))

    monkeypatch.setattr(bot.github, "deployment_report", _deployment_report)

    await bot._cmd_version(repos, slack.ack, slack.respond, _command("my-app"), {})

    said = slack.said
    assert "• RC: 2026.10.8.1 · " in said
    assert "↳ newer: 2026.10.9.1 started 20m ago, not successful (failure)" in said
    # Even with no success in the scanned window, the attempt is reported.
    assert "• Production: no successful deployment recorded" in said
    assert "not successful (no status)" in said


async def test_version_without_an_app_covers_every_migrated_app(
    mixed_repos, slack, monkeypatch
):
    """Legacy apps record no Deployments; they are named, not queried."""
    seen = []

    async def _deployment_report(repo, _environment):
        seen.append(repo)
        return _report()

    monkeypatch.setattr(bot.github, "deployment_report", _deployment_report)

    await bot._cmd_version(mixed_repos, slack.ack, slack.respond, _command(""), {})

    said = slack.said
    assert set(seen) == {"mitodl/my-app"}
    assert "*my-app*" in said
    assert "no successful deployment recorded" in said
    assert "Not shown -- the legacy pipeline records no deployments: legacy-app" in said


async def test_version_reports_one_environment_failing_without_hiding_the_other(
    repos, slack, monkeypatch
):
    async def _deployment_report(_repo, environment):
        if environment == "RC":
            msg = "GitHub is down"
            raise RuntimeError(msg)
        return _report(_live_deployment("2026.10.8.1", "a" * 40, "Production"))

    monkeypatch.setattr(bot.github, "deployment_report", _deployment_report)

    await bot._cmd_version(repos, slack.ack, slack.respond, _command("my-app"), {})

    assert "• Production: 2026.10.8.1" in slack.said
    assert "• RC: ⚠️ could not read its deployments" in slack.said


async def test_version_refuses_a_legacy_app(mixed_repos, slack, monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(bot.github, "deployment_report", report)

    await bot._cmd_version(
        mixed_repos, slack.ack, slack.respond, _command("legacy-app"), {}
    )

    report.assert_not_awaited()
    assert "legacy" in slack.said


async def test_help_and_uptime_reply_privately_release_state_does_not(
    repos, slack, monkeypatch
):
    monkeypatch.setattr(
        bot.github, "deployment_report", AsyncMock(return_value=_report())
    )
    for text in ("help", "uptime", "version my-app"):
        slack.respond.reset_mock()
        await bot._cmd_doof(repos, slack.ack, slack.respond, _command(text), {})
        kwargs = slack.respond.call_args.kwargs
        if text == "version my-app":
            assert kwargs.get("response_type") == "in_channel"
        else:
            assert "response_type" not in kwargs


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), "0s"),
        (timedelta(seconds=42), "42s"),
        (timedelta(minutes=5, seconds=2), "5m 2s"),
        (timedelta(hours=2, minutes=5, seconds=9), "2h 5m"),
        (timedelta(days=3, hours=4, minutes=1), "3d 4h"),
        (timedelta(days=1, minutes=7), "1d 7m"),
        (timedelta(seconds=-5), "0s"),
    ],
)
def test_format_elapsed(elapsed, expected):
    assert bot._format_elapsed(elapsed) == expected


@pytest.fixture
def _clean_heartbeats():
    bot._poll_heartbeats.clear()
    yield
    bot._poll_heartbeats.clear()


@pytest.mark.usefixtures("_clean_heartbeats")
async def test_uptime_reports_process_age_and_each_poller(repos, slack, monkeypatch):
    now = datetime.now(tz=UTC)
    monkeypatch.setattr(bot, "_started_at", now - timedelta(days=2, hours=3))
    monkeypatch.setenv("HOSTNAME", "release-bot-production-abc")
    bot._poll_heartbeats["ready-to-promote"] = (now - timedelta(seconds=30), True)
    bot._poll_heartbeats["release-progress"] = (now - timedelta(minutes=1), False)

    await bot._cmd_uptime(repos, slack.ack, slack.respond, _command(""), {})

    said = slack.said
    assert "Up 2d 3h" in said
    assert "`release-bot-production-abc`" in said
    assert "ready-to-promote poller: last pass 30s ago" in said
    assert "release-progress poller: last pass 1m" in said
    assert "some of its calls failed; see the bot's logs" in said
    assert "overdue" not in said


@pytest.mark.usefixtures("_clean_heartbeats")
async def test_uptime_flags_a_wedged_poller(repos, slack):
    """A loop stuck inside an await stops beating; say so."""
    stale = datetime.now(tz=UTC) - timedelta(
        seconds=bot._READY_TO_PROMOTE_POLL_SECONDS * bot._POLL_OVERDUE_INTERVALS + 60
    )
    bot._poll_heartbeats["ready-to-promote"] = (stale, True)

    await bot._cmd_uptime(repos, slack.ack, slack.respond, _command(""), {})

    said = slack.said
    assert "ready-to-promote poller: last pass" in said
    assert "overdue (runs every 2m)" in said
    assert "release-progress poller: has not finished a pass yet" in said
    # Freshly started: not having finished a pass yet is not a fault.
    assert said.count("overdue") == 1


@pytest.mark.usefixtures("_clean_heartbeats")
async def test_uptime_flags_a_first_pass_that_never_finished(repos, slack, monkeypatch):
    """A first pass wedged inside an await never beats at all."""
    monkeypatch.setattr(bot, "_started_at", datetime.now(tz=UTC) - timedelta(days=3))

    await bot._cmd_uptime(repos, slack.ack, slack.respond, _command(""), {})

    said = slack.said
    assert "ready-to-promote poller: has not finished a pass yet ⚠️ overdue" in said
    assert "release-progress poller: has not finished a pass yet ⚠️ overdue" in said


@pytest.mark.usefixtures("_clean_heartbeats")
async def test_poll_loops_record_a_heartbeat_even_when_an_iteration_fails(
    repos, monkeypatch
):
    monkeypatch.setattr(
        bot, "_notify_ready_to_promote", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(bot, "_announce_deployments", AsyncMock(return_value=True))
    monkeypatch.setattr(bot, "_nag_stuck_releases", AsyncMock(return_value=True))

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.asyncio, "sleep", _stop)

    for loop in (bot._poll_ready_to_promote_loop, bot._poll_release_progress_loop):
        with pytest.raises(asyncio.CancelledError):
            await loop(MagicMock(), repos)

    assert bot._poll_heartbeats["ready-to-promote"][1] is False
    assert bot._poll_heartbeats["release-progress"][1] is True


@pytest.mark.usefixtures("_clean_heartbeats")
async def test_a_pass_whose_per_app_calls_all_failed_is_not_reported_healthy(
    repos, monkeypatch
):
    """Per-app failures are caught so one app cannot starve the rest.

    That is exactly why nothing raises when *every* call fails -- a revoked
    GitHub App key, GitHub down -- and the step's own result has to carry it.
    """
    monkeypatch.setattr(
        bot.github,
        "open_release_issues",
        AsyncMock(side_effect=RuntimeError("401 Bad credentials")),
    )
    monkeypatch.setattr(
        bot.github,
        "latest_successful_deployment",
        AsyncMock(side_effect=RuntimeError("401 Bad credentials")),
    )
    monkeypatch.setattr(
        bot.github,
        "in_flight_release",
        AsyncMock(side_effect=RuntimeError("401 Bad credentials")),
    )

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.asyncio, "sleep", _stop)

    for loop in (bot._poll_ready_to_promote_loop, bot._poll_release_progress_loop):
        with pytest.raises(asyncio.CancelledError):
            await loop(MagicMock(), repos)

    assert bot._poll_heartbeats["ready-to-promote"][1] is False
    assert bot._poll_heartbeats["release-progress"][1] is False


async def test_a_clean_pass_reports_success(repos, monkeypatch):
    monkeypatch.setattr(bot.github, "open_release_issues", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bot.github, "latest_successful_deployment", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(bot.github, "in_flight_release", AsyncMock(return_value=None))
    state = bot.ReleaseProgressState()

    assert await bot._notify_ready_to_promote(MagicMock(), repos) is True
    assert await bot._announce_deployments(MagicMock(), repos, state) is True
    assert await bot._nag_stuck_releases(MagicMock(), repos, state) is True
