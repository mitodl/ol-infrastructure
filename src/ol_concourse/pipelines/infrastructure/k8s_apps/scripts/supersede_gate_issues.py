"""Close every open gate issue that no longer describes what would deploy.

Runs in the QA job after the release issue put. ``classify-promotion`` has just
decided the one issue that describes what a Production deploy would ship, and
the put has opened or updated it (or posted nothing, when there is nothing to
approve). Any other open issue for this app is stale: an infrastructure issue
opened before a release was cut, an older release that a newer cut replaced,
or an infrastructure issue titled with a live version that has since moved.
Closing one of those would fire the release gate.

So each one is closed, labelled ``abandoned`` and given a comment that points
at the issue that replaced it, creating the label first if the repository lacks
it. The release gate skips ``abandoned`` issues, so once the label lands a
superseded issue cannot fire it, even if someone closes it afterwards.

A gate issue closed as *not planned*, updated in the last day, and not yet
consumed or labelled is labelled too. This step closes issues that way, so a
retry finds one an earlier attempt closed but could not label; a person
discarding an issue that way gets the same protection. An issue closed as
*completed* is an approval and is never touched, even when a later QA run has
posted a newer issue for the same release: whether that approval still covers
what would deploy is the Production job's call, not this step's. A gate check
that already saw an issue closed before its label landed can still fire on it;
the Production job's ``assert-gate-names-deploy`` check is what guarantees
nothing unapproved deploys. This keeps the queue down to the one issue that
can be approved.

The put also posts nothing when the release it would announce was abandoned:
it skips when classify-promotion's ``release_tag`` is no longer in the
repository, which is what an abandoned release looks like until a newer one is
built. Then no skip marker exists and the put reports no issue. This confirms
the tag is gone and treats the run as having nothing to approve. If the tag is
still there, it refuses, as for any put that should have posted.

Only issues whose whole title is one of the two gate shapes are touched, the
same match ``assert_gate_names_deploy.py`` uses. ``mit-learn`` and
``mit-learn-nextjs`` share a repository, and a prefix match would close the
other app's issues. Consumed issues (``[CONSUMED #n]...``) and pull requests
never match.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained. It authenticates as the
release GitHub App, signing the app JWT with ``openssl``, which the task image
carries.

Environment:
    APP_NAME: the app.
    GITHUB_REPOSITORY: ``owner/repo`` holding the app's release issues.
    PROMOTION_DIR: classify-promotion's output (``title``, ``release_tag``
        and maybe ``skip``).
    ISSUE_FILE: ``<release-issue>/gh_issue.json`` from the issue put.
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
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ABANDONED_LABEL = "abandoned"
#: An issue as the GitHub REST API returns it.
Issue = dict[str, Any]
#: How far back to look for stale gate issues closed but not yet labelled.
CLOSED_LOOKBACK = timedelta(days=1)
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


def is_gate_title(app: str, title: str) -> bool:
    """Return whether *title* is one of this app's release gate titles.

    Kept in step with ``approved_version`` in ``assert_gate_names_deploy.py``.
    """
    shapes = (
        rf"Release {re.escape(app)} infrastructure @ \S+",
        rf"Release {re.escape(app)} \S+",
    )
    return any(re.fullmatch(shape, title.strip()) for shape in shapes)


def _label_names(issue: Issue) -> set[str]:
    return {label["name"] for label in issue.get("labels", [])}


def stale_issues(app: str, issues: list[Issue], keep_number: int | None) -> list[Issue]:
    """Return the gate issues to supersede, oldest first.

    Every open gate issue but *keep_number*, and every one closed as not
    planned that is not yet labelled ``abandoned``: the gate has not consumed
    it (its title would carry the ``[CONSUMED`` tombstone), so it would still
    fire. A *completed* close is an approval and is left alone. *keep_number*
    is the issue that describes what would deploy, or None when nothing does.
    """
    unique = {issue["number"]: issue for issue in issues}
    return sorted(
        (
            issue
            for issue in unique.values()
            if "pull_request" not in issue
            and issue["number"] != keep_number
            and is_gate_title(app, issue["title"])
            and (
                issue.get("state", "open") == "open"
                or (
                    issue.get("state_reason") == "not_planned"
                    and ABANDONED_LABEL not in _label_names(issue)
                )
            )
        ),
        key=lambda issue: issue["number"],
    )


def comment_for(keep: Issue | None, nothing_to_approve: str) -> str:
    """Return why an issue was superseded."""
    if keep is None:
        return (
            f"Superseded: {nothing_to_approve} There is nothing for this issue "
            "to approve."
        )
    return (
        f"Superseded by #{keep['number']} ({keep['title']}), which describes "
        "what a Production deploy would ship now."
    )


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
        self, method: str, path_or_url: str, body: Issue | None = None
    ) -> tuple[Any, dict[str, str]]:
        """Send one request; return the decoded body and the response headers."""
        # Pagination hands back absolute URLs; everything else is a path.
        url = path_or_url if "://" in path_or_url else f"{self.api_url}{path_or_url}"
        request = urllib.request.Request(  # noqa: S310
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self.auth,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ol-concourse-supersede-gate-issues",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            # Header names are case-insensitive; HTTP/2 sends them lowercase.
            headers = {key.lower(): value for key, value in response.headers.items()}
            return (json.loads(raw) if raw else None), headers

    def issues(self, repo: str, query: str) -> list[Issue]:
        """Return every issue in *repo* matching *query*, following pagination.

        The issues API, not search: search's index lags issue creation, and the
        issue to keep was opened moments ago.
        """
        issues: list[Issue] = []
        url: str | None = f"/repos/{repo}/issues?{query}&per_page=100"
        while url:
            page, headers = self.request("GET", url)
            issues.extend(page)
            match = _NEXT_LINK.search(headers.get("link", ""))
            url = match.group(1) if match else None
        return issues


def ensure_abandoned_label(github: GitHub, repo: str) -> None:
    """Create the ``abandoned`` label in *repo* unless it already exists."""
    # GitHub's REST API creates a missing label implicitly when an issue is
    # labelled with it, in practice, but does not document that. This label is
    # what stops the gate, so create it explicitly. 422 means it exists.
    try:
        github.request(
            "POST",
            f"/repos/{repo}/labels",
            {
                "name": ABANDONED_LABEL,
                "color": "ededed",
                "description": (
                    "Superseded or abandoned release gate issue; "
                    "the release gate ignores it."
                ),
            },
        )
    except urllib.error.HTTPError as error:
        if error.code != 422:  # noqa: PLR2004
            raise


def installation_token(
    api_url: str, app_id: str, installation_id: str, private_key: str
) -> str:
    """Exchange the app JWT for an installation token."""
    app = GitHub(api_url, app_jwt(app_id, private_key), scheme="Bearer")
    body, _ = app.request("POST", f"/app/installations/{installation_id}/access_tokens")
    return str(body["token"])


def supersede(github: GitHub, repo: str, issue: Issue, reason: str) -> None:
    """Comment on *issue*, then close it if open and label it ``abandoned``.

    An open issue is closed and labelled in one PATCH, so no reader sees half
    of it: closed but unlabelled would fire the gate, and open but labelled
    would let the issue put's ``update_in_place`` strip the label as stale.

    Raises RuntimeError if the label cannot be confirmed. The issue is then
    closed and unlabelled, and a retry finds it again among the closed ones.
    """
    number = issue["number"]
    is_open = issue.get("state", "open") == "open"
    action = "Closed and labelled" if is_open else "Labelled"
    comment = (
        f"{reason}\n\n{action} `{ABANDONED_LABEL}` by the QA job so the release "
        "gate ignores this issue. Approve the release by closing the issue that "
        "superseded it, not this one."
    )
    github.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": comment})
    if is_open:
        labels = sorted(_label_names(issue) | {ABANDONED_LABEL})
        github.request(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            {"state": "closed", "state_reason": "not_planned", "labels": labels},
        )
    else:
        github.request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            {"labels": [ABANDONED_LABEL]},
        )
    # Re-read: the label is what stops the gate, so confirm it survived
    # another writer, and put it back if not.
    for _ in range(3):
        current, _headers = github.request("GET", f"/repos/{repo}/issues/{number}")
        if ABANDONED_LABEL in _label_names(current):
            return
        github.request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            {"labels": [ABANDONED_LABEL]},
        )
    msg = f"#{number} is closed but its `{ABANDONED_LABEL}` label keeps disappearing"
    raise RuntimeError(msg)


class ReleaseOverError(Exception):
    """The put posted nothing because the release's tag may be gone."""

    def __init__(self, tag: str) -> None:
        """Name the tag whose absence would explain the empty put."""
        super().__init__(tag)
        self.tag = tag


def kept_issue(promotion_dir: Path, issue_file: Path) -> Issue | None:
    """Return the issue the put posted, or None when it posted nothing.

    Raises ReleaseOverError when the put names no issue but was told to skip
    if ``release_tag`` is missing: the caller has to confirm that it is.
    Raises ValueError when the put should have posted one but names none:
    superseding everything then would close the issue that approves the
    release.
    """
    if (promotion_dir / "skip").exists():
        return None
    posted = json.loads(issue_file.read_text())
    number = int(posted.get("issue_number") or 0)
    if number > 0:
        return {"number": number, "title": posted.get("issue_title", "")}
    tag_file = promotion_dir / "release_tag"
    tag = tag_file.read_text().strip() if tag_file.exists() else ""
    if tag:
        raise ReleaseOverError(tag)
    msg = f"the release issue put reported no issue in {issue_file}"
    raise ValueError(msg)


def tag_exists(github: GitHub, repo: str, tag: str) -> bool:
    """Return whether the tag *tag* is in *repo*; any error but a 404 propagates.

    The single-ref endpoint matches the name exactly, unlike matching-refs.
    """
    try:
        github.request("GET", f"/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}")
    except urllib.error.HTTPError as error:
        if error.code == 404:  # noqa: PLR2004
            return False
        raise
    return True


def main() -> None:
    """Supersede every open gate issue other than the one just posted."""
    app = os.environ["APP_NAME"]
    repo = os.environ["GITHUB_REPOSITORY"]
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    promotion_dir = Path(os.environ["PROMOTION_DIR"])
    nothing_to_approve = (
        "Production already matches this code: no release is waiting and the "
        "Production preview shows no changes."
    )
    over: str | None = None
    try:
        keep = kept_issue(promotion_dir, Path(os.environ["ISSUE_FILE"]))
    except ReleaseOverError as error:
        keep, over = None, error.tag
    except (ValueError, OSError) as error:
        sys.exit(f"Refusing to supersede {app}'s gate issues: {error}.")

    token = installation_token(
        api_url,
        os.environ["GITHUB_APP_ID"],
        os.environ["GITHUB_APP_INSTALLATION_ID"],
        os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    github = GitHub(api_url, token)
    if over is not None:
        if tag_exists(github, repo, over):
            sys.exit(
                f"Refusing to supersede {app}'s gate issues: the release issue "
                f"put posted nothing, but tag {over} is still in {repo}."
            )
        print(f"{app}: tag {over} is gone, so its release was abandoned.")  # noqa: T201
        nothing_to_approve = (
            f"Release {over} was abandoned (its tag is gone), and nothing can be "
            "approved until a new release is cut."
        )
    since = (datetime.now(UTC) - CLOSED_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates = [
        *github.issues(repo, "state=open"),
        *github.issues(repo, f"state=closed&since={since}"),
    ]
    stale = stale_issues(app, candidates, keep["number"] if keep else None)
    if not stale:
        print(f"{app}: no stale gate issues.")  # noqa: T201
        return

    reason = comment_for(keep, nothing_to_approve)
    ensure_abandoned_label(github, repo)
    for issue in stale:
        print(f"{app}: superseding #{issue['number']} ({issue['title']!r}).")  # noqa: T201
        supersede(github, repo, issue, reason)


if __name__ == "__main__":
    main()
