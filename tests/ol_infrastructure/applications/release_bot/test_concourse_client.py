"""Tests for release_bot's Concourse REST client.

`check_resource` must not return until the check it started has finished. The
POST endpoint answers as soon as the check is *created*, so returning then
would let a caller trigger a job before the new resource version is recorded
-- the stale-version binding the release flow exists to prevent.

Concourse 8.2.5 returns the check build as JSON but mislabels it
`text/plain`, because `CheckResource` commits the `201` before encoding the
body. Plain `resp.json()` rejects that on mimetype -- which is what took the
command down in production -- while `content_type=None` decodes it fine. The
fake below models exactly that: it raises under aiohttp's default validation
and returns the build when validation is disabled.
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
    """A response whose body is JSON but whose mimetype may not say so.

    When `mislabeled` is set, `json()` behaves like aiohttp against Concourse
    8.2.5: raising `ContentTypeError` under default validation, and returning
    the decoded body when the caller passes `content_type=None`.
    """

    _UNSET = object()

    def __init__(self, payload, *, mislabeled=False):
        self._payload = payload
        self._mislabeled = mislabeled

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def json(self, content_type=_UNSET):
        if self._mislabeled and content_type is not None:
            raise aiohttp.ContentTypeError(None, None)
        if self._payload is None:
            msg = "no body"
            raise ValueError(msg)
        return self._payload


class _FakeSession:
    """Serves GET payloads by URL kind, and one POST payload."""

    def __init__(
        self, post_payload, *, resource_states=(), build_states=(), mislabeled=False
    ):
        self.post_payload = post_payload
        self.mislabeled = mislabeled
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
        return _FakeResponse(self.post_payload, mislabeled=self.mislabeled)

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
# Concourse 8.2.5: a JSON build mislabeled as text/plain
# ---------------------------------------------------------------------------


async def test_check_resource_decodes_the_mislabeled_build_and_polls_it(monkeypatch):
    """The production failure, and what the real response actually looks like.

    `CheckResource` writes the `201` before encoding the build, so the body is
    JSON under a `text/plain` content type. Plain `resp.json()` raised
    `ContentTypeError` on that, so `/doof release` reported it could not
    refresh the version and refused to trigger -- even though the check had
    succeeded. Decoding with `content_type=None` yields the build, which is
    then polled to a terminal status.
    """
    session = _install(
        monkeypatch,
        _FakeSession(
            {"id": 42, "status": "started"},
            mislabeled=True,
            resource_states=[{"last_checked": 100}],
            build_states=[{"status": "started"}, {"status": "succeeded"}],
        ),
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    assert [u for u in session.get_urls if "/builds/42" in u], (
        "the build from the mislabeled body must actually be polled"
    )


@pytest.mark.parametrize("status", ["failed", "errored", "aborted"])
async def test_check_resource_raises_when_the_build_does_not_succeed(
    monkeypatch, status
):
    """A check that did not succeed must never read as a refreshed version."""
    _install(
        monkeypatch,
        _FakeSession(
            {"id": 42},
            mislabeled=True,
            resource_states=[{"last_checked": 100}],
            build_states=[{"status": status}],
        ),
    )
    with pytest.raises(RuntimeError, match=status):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_times_out_rather_than_polling_forever(monkeypatch):
    """A check that never lands must surface, not hang the Slack handler."""
    _install(
        monkeypatch,
        _FakeSession(
            {"id": 42},
            mislabeled=True,
            resource_states=[{"last_checked": 100}],
            build_states=[{"status": "started"}] * 5,
        ),
    )
    readings = {"n": 0}

    def fake_monotonic():
        readings["n"] += 1
        return 0 if readings["n"] == 1 else 10_000

    monkeypatch.setattr(concourse.time, "monotonic", fake_monotonic)
    with pytest.raises(RuntimeError, match="did not finish"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


# ---------------------------------------------------------------------------
# Fallback: a Concourse that returns no usable build
# ---------------------------------------------------------------------------


async def test_check_resource_falls_back_to_the_resource_build_summary(monkeypatch):
    """With no build in the response, the resource's own summary decides.

    `atc.Resource` carries `last_checked` and a `build` summary -- and no
    `check_error` field, so the summary's status is the only outcome signal.
    """
    session = _install(
        monkeypatch,
        _FakeSession(
            None,
            resource_states=[
                {"last_checked": 100, "build": {"status": "succeeded"}},
                {"last_checked": 100, "build": {"status": "succeeded"}},
                {"last_checked": 250, "build": {"status": "succeeded"}},
            ],
        ),
    )
    await concourse.check_resource("my-app-pipeline", "my-app-release")
    assert not any("/builds/" in u for u in session.get_urls)


async def test_check_resource_fallback_raises_on_a_failed_check(monkeypatch):
    """A bare `last_checked` bump must not be read as success.

    A failed check advances `last_checked` exactly like a successful one, so
    returning on the timestamp alone would let `/doof release` trigger from a
    resource whose check had just errored.
    """
    _install(
        monkeypatch,
        _FakeSession(
            None,
            resource_states=[
                {"last_checked": 100, "build": {"status": "succeeded"}},
                {"last_checked": 250, "build": {"status": "errored"}},
            ],
        ),
    )
    with pytest.raises(RuntimeError, match="errored"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


async def test_check_resource_fallback_ignores_the_previous_checks_summary(
    monkeypatch,
):
    """Until `last_checked` advances the summary still describes the old check."""
    _install(
        monkeypatch,
        _FakeSession(
            None,
            resource_states=[
                # Previous check failed; this one has not landed yet.
                {"last_checked": 100, "build": {"status": "failed"}},
            ],
        ),
    )
    readings = {"n": 0}

    def fake_monotonic():
        readings["n"] += 1
        return 0 if readings["n"] == 1 else 10_000

    monkeypatch.setattr(concourse.time, "monotonic", fake_monotonic)
    # Times out rather than inheriting the stale "failed" verdict.
    with pytest.raises(RuntimeError, match="did not finish"):
        await concourse.check_resource("my-app-pipeline", "my-app-release")


# ---------------------------------------------------------------------------
# Per-call team, for the library publish pipelines
# ---------------------------------------------------------------------------


class _PlainResponse:
    """Returns its payload verbatim, `None` included.

    Distinct from `_FakeResponse`, which models the mislabeled *check* body and
    so treats `None` as "no body at all". Concourse's `/jobs` endpoint really
    can answer JSON `null` -- Go marshals a nil slice that way -- and that is a
    pipeline with no jobs, not a missing body.
    """

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


class _UrlRecordingSession:
    """Records every URL and answers with one canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, url, **_kwargs):
        self.urls.append(url)
        return _PlainResponse(self.payload)

    get = _record
    post = _record


async def test_trigger_job_targets_the_team_it_is_given(monkeypatch):
    """The bot's own team owns the app pipelines, not the publish pipelines.

    `CONCOURSE_TEAM` is `infrastructure` in the deployment; every library
    publish pipeline lives in team `main`. Triggering one under the ambient
    default 404s, which is half of why `/doof publish` never worked.
    """
    session = _install(monkeypatch, _UrlRecordingSession({"id": 7}))
    url = await concourse.trigger_job("publish-ol-django-pypi", "build-mail", "main")
    assert (
        "/teams/main/pipelines/publish-ol-django-pypi/jobs/build-mail/builds"
        in session.urls[0]
    )
    assert url.endswith("/builds/7")


async def test_trigger_job_falls_back_to_the_ambient_team(monkeypatch):
    """Omitting the team keeps the app-release call sites unchanged."""
    session = _install(monkeypatch, _UrlRecordingSession({"id": 1}))
    await concourse.trigger_job("my-app-pipeline", "build-my-app-release-image")
    assert (
        f"/teams/{concourse.CONCOURSE_TEAM}/pipelines/my-app-pipeline/"
        in session.urls[0]
    )


async def test_list_jobs_returns_job_names_in_pipeline_order(monkeypatch):
    """Monorepo packages are read off the pipeline, never from a static list."""
    session = _install(
        monkeypatch,
        _UrlRecordingSession([{"name": "build-mail"}, {"name": "build-scim"}]),
    )
    assert await concourse.list_jobs("publish-ol-django-pypi", "main") == [
        "build-mail",
        "build-scim",
    ]
    assert session.urls[0].endswith("/teams/main/pipelines/publish-ol-django-pypi/jobs")


async def test_list_jobs_tolerates_an_empty_pipeline(monkeypatch):
    """A null body is a pipeline with no jobs, not a crash."""
    _install(monkeypatch, _UrlRecordingSession(None))
    assert await concourse.list_jobs("publish-ol-django-pypi", "main") == []


async def test_pipeline_is_paused_reports_the_paused_flag(monkeypatch):
    """publish-ol-django-pypi is paused, so this is the first thing a publish hits."""
    _install(monkeypatch, _UrlRecordingSession({"name": "p", "paused": True}))
    assert await concourse.pipeline_is_paused("publish-ol-django-pypi", "main") is True


async def test_pipeline_is_paused_is_false_when_the_flag_is_absent(monkeypatch):
    _install(monkeypatch, _UrlRecordingSession({"name": "p"}))
    assert await concourse.pipeline_is_paused("mit-learn-api-client", "main") is False
