"""Concourse REST API client for the release bot."""

import asyncio
import os
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

CONCOURSE_URL = os.environ.get("CONCOURSE_URL", "https://cicd.odl.mit.edu")
CONCOURSE_TEAM = os.environ.get("CONCOURSE_TEAM", "main")
CONCOURSE_USER = os.environ.get("CONCOURSE_USER", "")
CONCOURSE_PASS = os.environ.get("CONCOURSE_PASSWORD", "")

# Concourse's skymarshal hardcodes this public OAuth2 client for the fly-login
# (resource-owner password credentials) grant -- not a secret, just how
# Concourse identifies "fly" as the calling client. See flyClientID/
# flyClientSecret in concourse/concourse's atc/atccmd/command.go.
_FLY_CLIENT_ID = "fly"
_FLY_CLIENT_SECRET = "Zmx5"  # pragma: allowlist secret  # noqa: S105

_token: str | None = None
_token_expiry: float = 0.0
_token_lock = asyncio.Lock()


async def _get_token() -> str:
    global _token, _token_expiry  # noqa: PLW0603
    if _token and time.time() < _token_expiry:
        return _token

    async with _token_lock:
        # Re-check after acquiring the lock: another concurrent caller may
        # have already refreshed the token while we were waiting.
        if _token and time.time() < _token_expiry:
            return _token

        url = f"{CONCOURSE_URL}/sky/issuer/token"
        data = {
            "grant_type": "password",
            "username": CONCOURSE_USER,
            "password": CONCOURSE_PASS,
            "scope": "openid profile email federated:id groups",
        }
        auth = aiohttp.BasicAuth(_FLY_CLIENT_ID, _FLY_CLIENT_SECRET)
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, data=data, auth=auth) as resp,
        ):
            resp.raise_for_status()
            body = await resp.json()

        _token = body["access_token"]
        expires_in = body.get("expires_in", 86400)
        _token_expiry = time.time() + expires_in - 60  # refresh 60s before expiry
        return _token


async def trigger_job(pipeline: str, job: str, team: str | None = None) -> str:
    """Trigger a Concourse job and return the build URL.

    :param team: Concourse team owning *pipeline*. Defaults to the bot's
        ``CONCOURSE_TEAM`` (``infrastructure``, where the per-app release
        pipelines live). Library publish pipelines are in team ``main``, so
        they pass their own -- see ``bridge.settings.libraries``.
    """
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{team or CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/jobs/{job}/builds"
    )
    async with (
        aiohttp.ClientSession() as session,
        session.post(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        build = await resp.json()

    return f"{CONCOURSE_URL}/builds/{build['id']}"


def job_url(pipeline: str, job: str, team: str | None = None) -> str:
    """Return the web UI URL of *job*, whose newest build is listed first."""
    return (
        f"{CONCOURSE_URL}/teams/{team or CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/jobs/{job}"
    )


async def list_jobs(pipeline: str, team: str | None = None) -> list[str]:
    """Return the names of the jobs defined in *pipeline*, in pipeline order.

    Lets the bot resolve a monorepo's publishable packages from the pipeline
    itself instead of from a hand-maintained list. The publish pipelines
    discover their packages at generation time by walking the source checkout
    (``discover_python_packages``), so any list written down elsewhere is stale
    the moment a package is added.
    """
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{team or CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/jobs"
    )
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        jobs = await resp.json()
    return [job["name"] for job in jobs or []]


async def pipeline_is_paused(pipeline: str, team: str | None = None) -> bool:
    """Return whether *pipeline* is paused.

    A build triggered into a paused pipeline is created and then never
    scheduled, so the bot reports a build URL that sits at "pending" forever.
    Checking first lets a publish into a paused pipeline say so instead.
    """
    token = await _get_token()
    url = f"{CONCOURSE_URL}/api/v1/teams/{team or CONCOURSE_TEAM}/pipelines/{pipeline}"
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        body = await resp.json()
    return bool(body.get("paused"))


_BUILDS_PAGE_LIMIT = 100
_ACTIVE_BUILD_STATUSES = frozenset({"pending", "started"})


def _older_page_cursor(link: object) -> str | None:
    """Return the `to` build id of a `Link: rel="next"` URL, or None."""
    return parse_qs(urlsplit(str(link)).query).get("to", [None])[0]


async def active_build_url(
    pipeline: str, job: str, team: str | None = None
) -> str | None:
    """Return the URL of a pending or running build of *job*, or None if idle.

    Not the job's `next_build`. Concourse 8.3.1 leaves out reruns of any build
    older than the latest non-rerun one (`updateNextBuildForJob` in
    atc/db/build.go), so an old rerun can be running while it is empty. The
    job's build list includes them, but sorts each rerun beside the build it
    reruns rather than by when it started, so every page is read.

    Pages are followed by the `to` cursor in the `Link: rel="next"` header,
    sent back to `CONCOURSE_URL` rather than to the URL in the header, so the
    token only goes where every other call sends it.

    :raises RuntimeError: If the cursor does not move to older builds.
    """
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{team or CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/jobs/{job}/builds"
    )
    params: dict[str, str] = {"limit": str(_BUILDS_PAGE_LIMIT)}
    async with aiohttp.ClientSession() as session:
        while True:
            async with session.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params
            ) as resp:
                resp.raise_for_status()
                builds = await resp.json()
                older = resp.links.get("next")
            for build in builds or []:
                if build.get("status") in _ACTIVE_BUILD_STATUSES:
                    return f"{CONCOURSE_URL}/builds/{build['id']}"
            cursor = _older_page_cursor(older["url"]) if older else None
            if cursor is None:
                return None
            if cursor == params.get("to"):
                msg = f"Build list of {pipeline}/{job} repeated page to={cursor}."
                raise RuntimeError(msg)
            params = {"limit": str(_BUILDS_PAGE_LIMIT), "to": cursor}


_CHECK_POLL_SECONDS = 2
_CHECK_TIMEOUT_SECONDS = 180
_TERMINAL_BUILD_STATUSES = frozenset({"succeeded", "failed", "errored", "aborted"})


async def _get_build_status(build_id: int) -> str:
    token = await _get_token()
    url = f"{CONCOURSE_URL}/api/v1/builds/{build_id}"
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        build = await resp.json()
    return build["status"]


async def _get_resource(pipeline: str, resource: str) -> dict[str, Any]:
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/resources/{resource}"
    )
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        return await resp.json()


async def has_version(pipeline: str, resource: str, fields: dict[str, str]) -> bool:
    """Return whether *resource* has recorded a version containing *fields*.

    A check that succeeds has not necessarily found anything: it reports the
    same success whether or not the version a caller is waiting for was
    there to find. This is how a caller tells the two apart.

    Each `filter=key:value` narrows the version list to versions whose fields
    contain that exact pair (all of them, when repeated). Each returned
    version is matched again here, so a Concourse that ignored the filter and
    returned every version would not produce a false yes.
    """
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/resources/{resource}/versions"
    )
    params = [("filter", f"{key}:{value}") for key, value in fields.items()]
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            url, headers={"Authorization": f"Bearer {token}"}, params=params
        ) as resp,
    ):
        resp.raise_for_status()
        versions = await resp.json()
    return any(
        (entry.get("version") or {}).items() >= fields.items()
        for entry in versions or []
    )


async def _read_build(resp: aiohttp.ClientResponse) -> dict[str, Any] | None:
    """Return the check build from a response, or None if there isn't one.

    Concourse 8.2.5 *does* return the check build as JSON -- it is just
    mislabeled. `CheckResource` commits the status line before writing the
    body::

        w.WriteHeader(http.StatusCreated)
        err = json.NewEncoder(w).Encode(present.Build(build, nil, nil))

    so the headers flush with Go's default `text/plain; charset=utf-8` and the
    JSON follows. `content_type=None` disables aiohttp's MIME check and
    decodes it. Calling plain `resp.json()` here is what raised
    `ContentTypeError` in production -- the mimetype was wrong, not the body
    missing.

    Still tolerant of a genuinely absent or non-JSON body, since this is the
    only place that assumption is made and other Concourse versions differ.
    """
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


async def check_resource(pipeline: str, resource: str) -> None:
    """Run a resource check and wait for it to finish.

    Used to make a resource pick up new versions immediately rather than
    waiting for its `check_every` interval — after closing a release issue so
    the production `-release-gate` sees it right away, and before triggering a
    release so the build binds the current version.

    **Waits for the check to finish.** The POST only starts it and returns
    immediately (`fly check-resource` separately streams the check build's
    events unless `--async` is passed). Returning as soon as the POST came
    back would let a caller trigger a job before the new version was recorded,
    which is exactly the stale-version binding this is meant to prevent.

    The check build comes back in the POST body (see :func:`_read_build`) and
    is polled by id until it reaches a terminal status, which must be
    `succeeded`. If some other Concourse version returns no usable build, the
    resource's own `build` summary is polled instead -- `atc.Resource` in
    8.2.5 carries `last_checked` and `build`, and notably *no* `check_error`
    field, so the summary's status is the only outcome signal available there.
    A bare `last_checked` bump is not treated as success: a failed check
    advances it too.

    :raises RuntimeError: If the check does not succeed, or does not finish
        within `_CHECK_TIMEOUT_SECONDS`.
    """
    before = await _get_resource(pipeline, resource)
    last_checked = before.get("last_checked") or 0

    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/resources/{resource}/check"
    )
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            url, headers={"Authorization": f"Bearer {token}"}, json={}
        ) as resp,
    ):
        resp.raise_for_status()
        build = await _read_build(resp)

    build_id = build.get("id") if build else None
    deadline = time.monotonic() + _CHECK_TIMEOUT_SECONDS
    while True:
        if build_id is not None:
            status = await _get_build_status(build_id)
        else:
            # No build to poll. The resource's own summary describes the
            # latest check, but only once this check has actually landed --
            # before then it still describes the *previous* one.
            current = await _get_resource(pipeline, resource)
            status = None
            if (current.get("last_checked") or 0) > last_checked:
                status = (current.get("build") or {}).get("status")

        if status in _TERMINAL_BUILD_STATUSES:
            if status != "succeeded":
                where = f" {CONCOURSE_URL}/builds/{build_id}" if build_id else ""
                msg = f"Check of {pipeline}/{resource} {status}.{where}"
                raise RuntimeError(msg)
            return

        if time.monotonic() >= deadline:
            msg = (
                f"Check of {pipeline}/{resource} did not finish within "
                f"{_CHECK_TIMEOUT_SECONDS}s (last status: {status!r})."
            )
            raise RuntimeError(msg)
        await asyncio.sleep(_CHECK_POLL_SECONDS)
