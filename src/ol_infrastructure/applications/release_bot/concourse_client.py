"""Concourse REST API client for the release bot."""

import asyncio
import os
import time

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


async def check_resource(pipeline: str, resource: str) -> None:
    """Run a resource check and wait for it to finish.

    Used to make a resource pick up new versions immediately rather than
    waiting for its `check_every` interval — after closing a release issue so
    the production `-release-gate` sees it right away, and before triggering a
    release so the build binds the current version.

    **Waits for the check build to succeed.** The POST only creates the build
    and returns `201` straight away (`fly check-resource` separately streams
    that build's events unless `--async` is passed). Returning here as soon as
    the POST came back would let a caller trigger a job before the new version
    was recorded, which is exactly the stale-version binding this is meant to
    prevent.

    :raises RuntimeError: If the check build fails, is aborted, or does not
        finish within `_CHECK_TIMEOUT_SECONDS`.
    """
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
        build = await resp.json()

    # Older Concourse versions answer this endpoint with no build body. There
    # is nothing to wait on in that case, so preserve the previous behaviour
    # rather than failing.
    build_id = build.get("id") if isinstance(build, dict) else None
    if build_id is None:
        return

    deadline = time.monotonic() + _CHECK_TIMEOUT_SECONDS
    while True:
        status = await _get_build_status(build_id)
        if status in _TERMINAL_BUILD_STATUSES:
            break
        if time.monotonic() >= deadline:
            msg = (
                f"Check of {pipeline}/{resource} did not finish within "
                f"{_CHECK_TIMEOUT_SECONDS}s (last status: {status!r}). "
                f"{CONCOURSE_URL}/builds/{build_id}"
            )
            raise RuntimeError(msg)
        await asyncio.sleep(_CHECK_POLL_SECONDS)

    if status != "succeeded":
        msg = (
            f"Check of {pipeline}/{resource} {status}. "
            f"{CONCOURSE_URL}/builds/{build_id}"
        )
        raise RuntimeError(msg)
