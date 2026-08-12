"""Tests for release_bot's Slack command handlers.

The handlers take their Slack callables (ack/respond) as arguments, so they
can be driven directly with async stubs -- no Slack app or socket needed.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import bot
import bot_config
import pytest


@pytest.fixture
def repos():
    return {
        "my-app": bot_config.AppConfig(
            pipeline="my-app-pipeline",
            repo="mitodl/my-app",
            branch="main",
            channel="C123",
        )
    }


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


@pytest.fixture(autouse=True)
def _clear_watchers(monkeypatch):
    """Reset the module-global watcher state so it cannot leak between tests.

    `_slack_app` is set here because create_app() always populates it before
    the socket handler starts accepting commands, so a handler observing it
    unset is unreachable in the running bot.
    """
    monkeypatch.setattr(bot, "_slack_app", object())
    bot._checkbox_watchers.clear()
    yield
    bot._checkbox_watchers.clear()


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
    assert "bob@example.com, carol@example.com" in said
    assert "alice@example.com" not in said
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
