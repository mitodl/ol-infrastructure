"""Tests for release_bot's Concourse REST client.

`check_resource` must not return until the check build it created has
finished. The POST endpoint answers 201 as soon as the build is *created*, so
returning then would let a caller trigger a job before the new resource
version is recorded -- the stale-version binding the release flow exists to
prevent.
"""

from unittest.mock import AsyncMock

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

    async def json(self):
        return self._payload


class _FakeSession:
    """Returns the queued POST payload, then GET payloads in order."""

    def __init__(self, post_payload, get_payloads):
        self.post_payload = post_payload
        self.get_payloads = list(get_payloads)
        self.get_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, _url, **_kwargs):
        return _FakeResponse(self.post_payload)

    def get(self, url, **_kwargs):
        self.get_urls.append(url)
        return _FakeResponse(self.get_payloads.pop(0))


def _install(monkeypatch, post_payload, get_payloads):
    session = _FakeSession(post_payload, get_payloads)
    monkeypatch.setattr(concourse.aiohttp, "ClientSession", lambda *_a, **_kw: session)
    return session


async def test_check_resource_waits_for_the_build_to_succeed(monkeypatch):
    """Must poll past a running build rather than returning immediately."""
    session = _install(
        monkeypatch,
        {"id": 42},
        [{"status": "started"}, {"status": "started"}, {"status": "succeeded"}],
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    assert len(session.get_urls) == 3
    assert session.get_urls[0].endswith("/api/v1/builds/42")


async def test_check_resource_raises_when_the_check_fails(monkeypatch):
    """A failed check means the version was never refreshed -- do not swallow it."""
    _install(monkeypatch, {"id": 42}, [{"status": "failed"}])
    with pytest.raises(RuntimeError, match="failed"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_raises_when_the_check_errors(monkeypatch):
    _install(monkeypatch, {"id": 42}, [{"status": "errored"}])
    with pytest.raises(RuntimeError, match="errored"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_times_out_rather_than_polling_forever(monkeypatch):
    """A check stuck 'started' must surface, not hang the Slack handler."""
    _install(monkeypatch, {"id": 42}, [{"status": "started"}] * 50)
    # The first reading sets the deadline; every later one is past it. Must not
    # be an exhaustible iterator -- pytest's own teardown reads the clock too.
    readings = {"n": 0}

    def fake_monotonic():
        readings["n"] += 1
        return 0 if readings["n"] == 1 else 10_000

    monkeypatch.setattr(concourse.time, "monotonic", fake_monotonic)
    with pytest.raises(RuntimeError, match="did not finish"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_tolerates_a_bodyless_response(monkeypatch):
    """Older Concourse answers with no build; there is nothing to wait on."""
    session = _install(monkeypatch, {}, [])
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    assert session.get_urls == []
