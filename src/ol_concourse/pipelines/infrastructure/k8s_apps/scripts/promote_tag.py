"""Point the Production tag of a tag-only release at its release tag.

Runs in a tag-only release's Production job after the gate approves the
version. A tag-only release deploys nothing itself: its consumers check the
repository out at a tag. RC consumers track the newest ``<version>`` tag,
which the release resource pushes when it cuts a release. Production
consumers track the newest ``production/<version>`` tag, and this is the only
thing that writes one, so a release reaches Production only after its gate
issue is closed.

The Production tag names the same commit as the release tag, so Production
gets exactly what RC ran. An annotated release tag is followed to its commit.
Tags are immutable here: a re-run that finds the Production tag already at
that commit changes nothing, and one that finds it anywhere else fails rather
than moving it.

A version older than the newest Production tag is refused, even when its own
Production tag is already in place. Consumers run the newest tag, so
re-running an old Production build changes nothing they run, but its
Production Deployment would report the old version as live. The Production
job runs this with ``CHECK_ONLY`` set before it starts that Deployment, so
such a re-run fails before it records anything.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained. It authenticates as the
release GitHub App the same way ``label_gate_issue.py`` does, signing the app
JWT with ``openssl``, which the task image carries.

Environment:
    VERSION_FILE: the release resource's ``version`` file.
    TAG_PREFIX: prefix of the Production tag, e.g. ``production/``.
    GITHUB_REPOSITORY: ``owner/repo`` of the released repository.
    GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY: the
        release GitHub App's credentials.
    GITHUB_API_URL: optional, defaults to ``https://api.github.com``.
    CHECK_ONLY: optional; when non-empty, run every check but create nothing.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_HTTP_NOT_FOUND = 404
_HTTP_UNPROCESSABLE = 422
_VERSION = re.compile(r"[0-9]{4}\.[0-9]+\.[0-9]+\.[0-9]+")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key: str) -> str:
    """Return a GitHub App JWT signed RS256 with ``openssl``."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as key_file:
        key_file.write(private_key)
        key_file.flush()
        signature = subprocess.run(  # noqa: S603
            ["openssl", "dgst", "-sha256", "-sign", key_file.name],  # noqa: S607
            input=signing_input,
            capture_output=True,
            check=True,
        ).stdout
    return f"{header}.{payload}.{_b64(signature)}"


class GitHub:
    """The few GitHub REST calls this needs."""

    def __init__(self, api_url: str, token: str, *, scheme: str = "token") -> None:
        """Authenticate every request with *token* under *scheme*."""
        self.api_url = api_url.rstrip("/")
        self.auth = f"{scheme} {token}"

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Send one request and return the decoded body."""
        request = urllib.request.Request(  # noqa: S310
            f"{self.api_url}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self.auth,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ol-concourse-promote-tag",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None


def installation_token(
    api_url: str, app_id: str, installation_id: str, private_key: str
) -> str:
    """Exchange the app JWT for an installation token."""
    app = GitHub(api_url, app_jwt(app_id, private_key), scheme="Bearer")
    body = app.request("POST", f"/app/installations/{installation_id}/access_tokens")
    return str(body["token"])


def tag_commit(github: GitHub, repo: str, tag: str) -> str | None:
    """Return the commit *tag* names, following an annotated tag; None if absent."""
    try:
        ref = github.request(
            "GET", f"/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}"
        )
    except urllib.error.HTTPError as error:
        if error.code == _HTTP_NOT_FOUND:
            return None
        raise
    target = ref["object"]
    while target["type"] == "tag":
        target = github.request("GET", f"/repos/{repo}/git/tags/{target['sha']}")[
            "object"
        ]
    return str(target["sha"])


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def newest_production_version(github: GitHub, repo: str, prefix: str) -> str | None:
    """Return the newest version with a ``<prefix><version>`` tag, if any.

    The matching-refs endpoint is not paginated; it returns every match.
    """
    refs = github.request(
        "GET",
        f"/repos/{repo}/git/matching-refs/tags/{urllib.parse.quote(prefix)}",
    )
    versions = [
        name
        for ref in refs or []
        if _VERSION.fullmatch(
            name := str(ref["ref"]).removeprefix(f"refs/tags/{prefix}")
        )
    ]
    return max(versions, key=_version_key, default=None)


def promote(
    github: GitHub, repo: str, version: str, prefix: str, *, check_only: bool = False
) -> str:
    """Create ``<prefix><version>`` at the commit tagged *version*; return a report.

    With *check_only*, stop after the checks and create nothing.

    Raises RuntimeError when the release tag is gone, a newer version already
    has a Production tag, or the Production tag already names another commit.
    """
    production_tag = f"{prefix}{version}"
    newest = newest_production_version(github, repo, prefix)
    if newest is not None and _version_key(newest) > _version_key(version):
        msg = (
            f"{prefix}{newest} is newer, and consumers run the newest Production "
            f"tag, so {version} cannot be put back in Production. Is this a "
            "re-run of an old build?"
        )
        raise RuntimeError(msg)
    commit = tag_commit(github, repo, version)
    if commit is None:
        msg = (
            f"release tag {version} is not in {repo}, so there is nothing to "
            "promote (was the release abandoned?)"
        )
        raise RuntimeError(msg)
    for _ in range(2):
        existing = tag_commit(github, repo, production_tag)
        if existing == commit:
            return f"{production_tag} already names {commit[:8]}; nothing to do."
        if existing is not None:
            msg = (
                f"{production_tag} already names {existing[:8]}, not the release "
                f"tag's {commit[:8]}. Production tags are never moved."
            )
            raise RuntimeError(msg)
        if check_only:
            return f"{production_tag} can be created at {commit[:8]}."
        try:
            github.request(
                "POST",
                f"/repos/{repo}/git/refs",
                {"ref": f"refs/tags/{production_tag}", "sha": commit},
            )
        except urllib.error.HTTPError as error:
            # Another run created it between the read and the write.
            if error.code != _HTTP_UNPROCESSABLE:
                raise
            continue
        return f"Created {production_tag} at {commit[:8]}."
    msg = f"could not create {production_tag}"
    raise RuntimeError(msg)


def main() -> None:
    """Create the Production tag for the release in ``VERSION_FILE``."""
    version = Path(os.environ["VERSION_FILE"]).read_text().strip()
    if not _VERSION.fullmatch(version):
        sys.exit(f"Not promoting: {version!r} is not a release version.")
    prefix = os.environ["TAG_PREFIX"]
    if not prefix or prefix.startswith("/") or ".." in prefix:
        sys.exit(f"Not promoting: {prefix!r} is not a usable tag prefix.")
    repo = os.environ["GITHUB_REPOSITORY"]
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    token = installation_token(
        api_url,
        os.environ["GITHUB_APP_ID"],
        os.environ["GITHUB_APP_INSTALLATION_ID"],
        os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    try:
        report = promote(
            GitHub(api_url, token),
            repo,
            version,
            prefix,
            check_only=bool(os.environ.get("CHECK_ONLY")),
        )
        print(report)  # noqa: T201
    except RuntimeError as error:
        sys.exit(f"Not promoting {repo} {version}: {error}.")


if __name__ == "__main__":
    main()
