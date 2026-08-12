"""Tests for release_bot's Concourse REST client.

`check_resource` must not return until the check it started has finished. The
POST endpoint answers as soon as the check is *created*, so returning then
would let a caller trigger a job before the new resource version is recorded
-- the stale-version binding the release flow exists to prevent.

The endpoint's response shape differs by Concourse version, and getting that
wrong took the command down in production: some versions return the check
build as JSON, Concourse 8.2.5 returns `201` with an empty `text/plain` body.
Both shapes are covered here.
"""

from unittest.mock import AsyncMock

import aiohttp
import concourse_client as concourse
import pytest


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    """Skip the OAuth round-trip; token handling is not under test here."""
    monkeypatch.setattr(concourse, "_get_token", AsyncMock(return_value="tok"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Poll without real delays."""
    monkeypatch.setattr(concourse.asyncio, "sleep", AsyncMock())


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def json(self, content_type=None):  # noqa: ARG002
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    """Serves GET payloads by URL kind, and one POST payload."""

    def __init__(self, post_payload, *, resource_states=(), build_states=()):
        self.post_payload = post_payload
        self.resource_states = list(resource_states)
        self.build_states = list(build_states)
        self.get_urls: list[str] = []
        self.post_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **_kwargs):
        self.post_urls.append(url)
        return _FakeResponse(self.post_payload)

    def get(self, url, **_kwargs):
        self.get_urls.append(url)
        if "/builds/" in url:
            return _FakeResponse(self.build_states.pop(0))
        # Resource reads repeat the final state once exhausted, so a test only
        # has to describe the transitions it cares about.
        state = (
            self.resource_states.pop(0)
            if len(self.resource_states) > 1
            else self.resource_states[0]
        )
        return _FakeResponse(state)


def _install(monkeypatch, session):
    monkeypatch.setattr(concourse.aiohttp, "ClientSession", lambda *_a, **_kw: session)
    return session


# ---------------------------------------------------------------------------
# Concourse 8.2.5: 201 with an empty text/plain body, no build to poll
# ---------------------------------------------------------------------------


async def test_check_resource_survives_a_bodyless_check_response(monkeypatch):
    """The production failure: `resp.json()` raised ContentTypeError.

    Concourse 8.2.5 answers the check endpoint with `201 text/plain` and no
    body. Calling `.json()` unconditionally raised before the "no build"
    branch could be reached, so `/doof release` reported that it could not
    refresh the version and refused to trigger -- even though the check itself
    had succeeded.
    """
    session = _install(
        monkeypatch,
        _FakeSession(
            aiohttp.ContentTypeError(None, None),
            resource_states=[{"last_checked": 100}, {"last_checked": 200}],
        ),
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    # Never tried to poll a build, since there was no build id to poll.
    assert not any("/builds/" in u for u in session.get_urls)


async def test_check_resource_waits_for_last_checked_to_advance(monkeypatch):
    """With no build to poll, `last_checked` is the completion signal."""
    session = _install(
        monkeypatch,
        _FakeSession(
            None,
            resource_states=[
                {"last_checked": 100},  # before the POST
                {"last_checked": 100},  # still running
                {"last_checked": 100},
                {"last_checked": 250},  # done
            ],
        ),
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    assert len(session.get_urls) == 4


async def test_check_resource_raises_on_a_resource_check_error(monkeypatch):
    """A resource whose check errored must not be reported as refreshed."""
    _install(
        monkeypatch,
        _FakeSession(
            None,
            resource_states=[
                {"last_checked": 100},
                {"last_checked": 100, "check_error": "no such ref"},
            ],
        ),
    )
    with pytest.raises(RuntimeError, match="no such ref"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_times_out_rather_than_polling_forever(monkeypatch):
    """A check that never lands must surface, not hang the Slack handler."""
    _install(
        monkeypatch,
        _FakeSession(None, resource_states=[{"last_checked": 100}]),
    )
    readings = {"n": 0}

    def fake_monotonic():
        readings["n"] += 1
        return 0 if readings["n"] == 1 else 10_000

    monkeypatch.setattr(concourse.time, "monotonic", fake_monotonic)
    with pytest.raises(RuntimeError, match="did not finish"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


# ---------------------------------------------------------------------------
# Versions that do return the check build as JSON
# ---------------------------------------------------------------------------


async def test_check_resource_polls_the_build_when_one_is_returned(monkeypatch):
    session = _install(
        monkeypatch,
        _FakeSession(
            {"id": 42},
            resource_states=[{"last_checked": 100}],
            build_states=[
                {"status": "started"},
                {"status": "started"},
                {"status": "succeeded"},
            ],
        ),
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    build_reads = [u for u in session.get_urls if "/builds/42" in u]
    assert len(build_reads) == 3


@pytest.mark.parametrize("status", ["failed", "errored", "aborted"])
async def test_check_resource_raises_when_the_build_does_not_succeed(
    monkeypatch, status
):
    _install(
        monkeypatch,
        _FakeSession(
            {"id": 42},
            resource_states=[{"last_checked": 100}],
            build_states=[{"status": status}],
        ),
    )
    with pytest.raises(RuntimeError, match=status):
        await concourse.check_resource("my-app-pipeline", "my-app-release")
