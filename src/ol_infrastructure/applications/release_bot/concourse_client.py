"""Concourse REST API client for the release bot."""

import asyncio
import os
import time
from typing import Any

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


async def trigger_job(pipeline: str, job: str) -> str:
    """Trigger a Concourse job and return the build URL."""
    token = await _get_token()
    url = (
        f"{CONCOURSE_URL}/api/v1/teams/{CONCOURSE_TEAM}"
        f"/pipelines/{pipeline}/jobs/{job}/builds"
    )
    async with (
        aiohttp.ClientSession() as session,
        session.post(url, headers={"Authorization": f"Bearer {token}"}) as resp,
    ):
        resp.raise_for_status()
        build = await resp.json()

    return f"{CONCOURSE_URL}/builds/{build['id']}"


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


async def _read_optional_json(
    resp: aiohttp.ClientResponse,
) -> dict[str, Any] | None:
    """Return the response body as a dict, or None when there isn't one.

    Concourse 8.2.5 answers the check endpoint with `201` and an empty
    `text/plain` body -- no build object at all. `resp.json()` raises
    `ContentTypeError` on that, so it cannot be called unconditionally and the
    "no body" case cannot be detected after the fact.
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

    Two shapes of response are handled, because they differ by Concourse
    version and this got it wrong in production: some return the check build
    as JSON, which can be polled by id; Concourse 8.2.5 returns `201` with an
    empty `text/plain` body and no build at all. Without a build to poll, the
    resource's own `last_checked` timestamp is what advances when the check
    completes, so that is waited on instead.

    :raises RuntimeError: If the check fails, is aborted, reports a check
        error, or does not finish within `_CHECK_TIMEOUT_SECONDS`.
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
        build = await _read_optional_json(resp)

    build_id = build.get("id") if build else None
    deadline = time.monotonic() + _CHECK_TIMEOUT_SECONDS
    while True:
        if build_id is not None:
            status = await _get_build_status(build_id)
            if status in _TERMINAL_BUILD_STATUSES:
                if status != "succeeded":
                    msg = (
                        f"Check of {pipeline}/{resource} {status}. "
                        f"{CONCOURSE_URL}/builds/{build_id}"
                    )
                    raise RuntimeError(msg)
                return
        else:
            current = await _get_resource(pipeline, resource)
            if error := current.get("check_error"):
                msg = f"Check of {pipeline}/{resource} failed: {error}"
                raise RuntimeError(msg)
            if (current.get("last_checked") or 0) > last_checked:
                return

        if time.monotonic() >= deadline:
            msg = (
                f"Check of {pipeline}/{resource} did not finish within "
                f"{_CHECK_TIMEOUT_SECONDS}s."
            )
            raise RuntimeError(msg)
        await asyncio.sleep(_CHECK_POLL_SECONDS)
