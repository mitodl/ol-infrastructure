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
