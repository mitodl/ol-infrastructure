"""Refuse a Production deploy that the closed gate issue did not approve.

Runs inside the Production job, before anything deploys. The release gate is
a trigger: closing any issue that matches its ``Release <app>`` prefix fires
the job, which then deploys whatever version last passed QA. Two issues can
both be open and match the prefix at once (a stale one left behind when a
newer release was cut, or a future infrastructure-only one), and closing the
wrong one would ship a release nobody approved. So this exits non-zero unless
the closed issue's own title names the version the job is about to deploy.

The title alone binds the app version, not the infrastructure. The issue shows
the Production preview of one ol-infrastructure commit, but the job deploys
whichever commit last passed QA, and that can be a later one: QA can preview a
newer commit after the issue is closed, or fail after posting the issue so that
the issue describes a commit no successful QA build produced. So the issue body
must also carry the revision marker ``classify_promotion.py`` writes, naming
the commit this job is about to deploy. The body is read from the GitHub API
because the gate get records the issue's title and number but not its body.

The body has to be the one the issue was closed with. The issue put finds the
issue to update through the search API, whose index lags, so a QA run that
lands just after the issue is closed can still rewrite the closed issue's
body, marker included. So the issue is refused if its body was edited at or
after it was closed. Labelling the issue, which the gate get does to consume
it, is not a body edit.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained. It authenticates as the
release GitHub App the same way ``supersede_gate_issues.py`` does, signing the
app JWT with ``openssl``, which the task image carries.

Environment:
    APP_NAME: the app whose release gate fired.
    GATE_FILE: ``<release-gate>/gh_issue.json`` from the gate get.
    VERSION_FILE: the file the job's ``image_tag`` comes from.
    INFRA_REF_FILE: ``<ol-infra>/.git/ref`` for the commit this job deploys.
    GITHUB_REPOSITORY: ``owner/repo`` holding the app's release issues.
    GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY: the
        release GitHub App's credentials.
    GITHUB_API_URL: optional, defaults to ``https://api.github.com``.
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
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

#: Kept in step with ``REVISION_MARKER`` in ``classify_promotion.py``.
_REVISION_MARKER = re.compile(r"<!-- ol-infrastructure-revision: ([0-9a-f]{40}) -->")
_SHA = re.compile(r"[0-9a-f]{40}")
_ATTEMPTS = 3
_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { body closed closedAt lastEditedAt }
  }
}
"""


def approved_version(app: str, title: str) -> str | None:
    """Return the version a gate issue title approves, or None if it names none.

    Accepts ``Release <app> <version>`` and ``Release <app> infrastructure @
    <version>``, matched in full so another app sharing the prefix (for
    example ``Release <app>-v2 <version>``) does not match.
    """
    shapes = (
        rf"Release {re.escape(app)} infrastructure @ (\S+)",
        rf"Release {re.escape(app)} (\S+)",
    )
    for shape in shapes:
        match = re.fullmatch(shape, title.strip())
        if match:
            return match.group(1)
    return None


def approved_revision(body: str) -> str | None:
    """Return the ol-infrastructure commit a gate issue body approves.

    None when the body names no revision, or names more than one: an edited
    body that no longer says which diff was approved approves nothing.
    """
    revisions = set(_REVISION_MARKER.findall(body or ""))
    return revisions.pop() if len(revisions) == 1 else None


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


def _request(api_url: str, method: str, path: str, auth: str, body: Any = None) -> Any:
    """Send one GitHub API request, retrying transport errors and 5xx."""
    request = urllib.request.Request(  # noqa: S310
        f"{api_url.rstrip('/')}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": auth,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ol-concourse-assert-gate-names-deploy",
            **({} if body is None else {"Content-Type": "application/json"}),
        },
    )
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == _ATTEMPTS:  # noqa: PLR2004
                raise
        except urllib.error.URLError:
            if attempt == _ATTEMPTS:
                raise
        time.sleep(2**attempt)
    msg = "unreachable"
    raise AssertionError(msg)


def edited_after_close(issue: dict[str, Any]) -> bool:
    """Return whether *issue*'s body may differ from the one it was closed with.

    True when it is not closed (``closed`` is read directly, so this does not
    depend on what ``closedAt`` says after a reopen), or its body was last
    edited at or after the close: timestamps have one-second resolution, so an
    edit in the same second as the close cannot be ordered and is not trusted.
    """
    closed = issue.get("closedAt")
    if issue.get("closed") is not True or not closed:
        return True
    edited = issue.get("lastEditedAt")
    if not edited:
        return False
    return _timestamp(edited) >= _timestamp(closed)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_issue(api_url: str, repo: str, number: int) -> dict[str, Any]:
    """Return issue *number*'s body and close/edit state, read as the release App."""
    jwt = app_jwt(os.environ["GITHUB_APP_ID"], os.environ["GITHUB_APP_PRIVATE_KEY"])
    installation = os.environ["GITHUB_APP_INSTALLATION_ID"]
    token = _request(
        api_url,
        "POST",
        f"/app/installations/{installation}/access_tokens",
        f"Bearer {jwt}",
    )["token"]
    owner, name = repo.split("/", 1)
    result = _request(
        api_url,
        "POST",
        "/graphql",
        f"bearer {token}",
        {
            "query": _ISSUE_QUERY,
            "variables": {"owner": owner, "name": name, "number": number},
        },
    )
    if result.get("errors"):
        msg = f"GitHub GraphQL errors reading #{number}: {result['errors']}"
        raise RuntimeError(msg)
    issue = ((result.get("data") or {}).get("repository") or {}).get("issue")
    if issue is None:
        msg = f"issue #{number} was not found in {repo}"
        raise RuntimeError(msg)
    return issue


def main() -> None:
    """Exit non-zero unless the gate issue approves this version and commit."""
    app = os.environ["APP_NAME"]
    with open(os.environ["GATE_FILE"]) as gate_file:  # noqa: PTH123
        gate = json.load(gate_file)
    title = (gate.get("issue_title") or "").strip()
    with open(os.environ["VERSION_FILE"]) as version_file:  # noqa: PTH123
        deploying = version_file.read().strip()

    approved = approved_version(app, title)
    if approved is None:
        sys.exit(
            f"Refusing to deploy {app} {deploying}: the gate issue that fired "
            f"({title!r}) does not name a version. Nothing was deployed."
        )
    if approved != deploying:
        sys.exit(
            f"Refusing to deploy {app} {deploying}: the gate issue that fired "
            f"({title!r}) approved {approved}. Nothing was deployed. To ship "
            f"{deploying}, close the release issue for {deploying}."
        )

    revision = Path(os.environ["INFRA_REF_FILE"]).read_text().strip()
    if not _SHA.fullmatch(revision):
        sys.exit(
            f"Refusing to deploy {app} {deploying}: the ol-infrastructure ref "
            f"{revision!r} is not a full commit SHA. Nothing was deployed."
        )
    number = int(gate.get("issue_number") or 0)
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    issue = fetch_issue(api_url, os.environ["GITHUB_REPOSITORY"], number)
    if edited_after_close(issue):
        sys.exit(
            f"Refusing to deploy {app} {deploying}: gate issue #{number} "
            f"({title!r}) is not closed, or its body was edited at or after it "
            f"was closed (closed {issue.get('closedAt')}, last edited "
            f"{issue.get('lastEditedAt')}), so it may not show what was "
            "approved. Nothing was deployed. Re-run the QA job to post an issue "
            "for what would deploy now, and close that one."
        )
    previewed = approved_revision(issue.get("body") or "")
    if previewed is None:
        sys.exit(
            f"Refusing to deploy {app} {deploying}: gate issue #{number} "
            f"({title!r}) does not say which ol-infrastructure commit it "
            "approved. Nothing was deployed. Re-run the QA job to post an "
            "issue that does, and close that one."
        )
    if previewed != revision:
        sys.exit(
            f"Refusing to deploy {app} {deploying} at ol-infrastructure "
            f"{revision[:8]}: gate issue #{number} ({title!r}) approved the "
            f"Production preview of {previewed[:8]}. Nothing was deployed. "
            f"Close the open release issue that previews {revision[:8]}, or "
            "re-run the QA job if there is none."
        )
    print(  # noqa: T201
        f"Gate issue #{number} ({title!r}) approves deploying {app} {deploying} "
        f"at ol-infrastructure {revision[:8]}."
    )


if __name__ == "__main__":
    main()
