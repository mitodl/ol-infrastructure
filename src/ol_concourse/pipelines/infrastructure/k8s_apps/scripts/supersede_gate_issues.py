"""Close every open gate issue that no longer describes what would deploy.

Runs in the QA job after the release issue put. ``classify-promotion`` has just
decided the one issue that describes what a Production deploy would ship, and
the put has opened or updated it (or posted nothing, when there is nothing to
approve). Any other open issue for this app is stale: an infrastructure issue
opened before a release was cut, an older release that a newer cut replaced,
or an infrastructure issue titled with a live version that has since moved.
Closing one of those would fire the release gate.

So each one is closed, labelled ``abandoned`` and given a comment that points
at the issue that replaced it. The release gate skips ``abandoned`` issues, so
a superseded issue cannot fire it even if someone closes it first. The
Production job's ``assert-gate-names-deploy`` check is still what guarantees
nothing unapproved deploys; this keeps the queue down to the one issue that
can be approved.

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
    PROMOTION_DIR: classify-promotion's output (``title`` and maybe ``skip``).
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
import urllib.request
from pathlib import Path
from typing import Any

ABANDONED_LABEL = "abandoned"
#: An issue as the GitHub REST API returns it.
Issue = dict[str, Any]
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


def stale_issues(
    app: str, open_issues: list[Issue], keep_number: int | None
) -> list[Issue]:
    """Return the open gate issues to supersede, oldest first.

    *keep_number* is the issue that describes what would deploy, or None when
    nothing does, in which case every open gate issue is stale.
    """
    return sorted(
        (
            issue
            for issue in open_issues
            if "pull_request" not in issue
            and issue["number"] != keep_number
            and is_gate_title(app, issue["title"])
        ),
        key=lambda issue: issue["number"],
    )


def comment_for(keep: Issue | None, nothing_to_approve: str) -> str:
    """Return the comment left on a superseded issue."""
    if keep is None:
        reason = (
            f"Superseded: {nothing_to_approve} There is nothing for this issue "
            "to approve."
        )
    else:
        reason = (
            f"Superseded by #{keep['number']} ({keep['title']}), which describes "
            "what a Production deploy would ship now."
        )
    return (
        f"{reason}\n\nClosed and labelled `{ABANDONED_LABEL}` by the QA job so "
        "the release gate ignores this issue. Approve the release by closing "
        "the issue that superseded it, not this one."
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
            return (json.loads(raw) if raw else None), dict(response.headers)

    def open_issues(self, repo: str) -> list[Issue]:
        """Return every open issue in *repo*, from the consistent issues API.

        Not search: its index lags issue creation, and the issue to keep was
        opened moments ago.
        """
        issues: list[Issue] = []
        url: str | None = f"/repos/{repo}/issues?state=open&per_page=100"
        while url:
            page, headers = self.request("GET", url)
            issues.extend(page)
            match = _NEXT_LINK.search(headers.get("Link", ""))
            url = match.group(1) if match else None
        return issues


def installation_token(
    api_url: str, app_id: str, installation_id: str, private_key: str
) -> str:
    """Exchange the app JWT for an installation token."""
    app = GitHub(api_url, app_jwt(app_id, private_key), scheme="Bearer")
    body, _ = app.request("POST", f"/app/installations/{installation_id}/access_tokens")
    return str(body["token"])


def supersede(github: GitHub, repo: str, issue: Issue, comment: str) -> None:
    """Comment on *issue*, then close and label it in one PATCH.

    One PATCH so no reader sees half of it: closed but unlabelled would fire
    the gate, and open but labelled would let the issue put's
    ``update_in_place`` strip the label as stale.
    """
    number = issue["number"]
    github.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": comment})
    labels = [label["name"] for label in issue.get("labels", [])]
    if ABANDONED_LABEL not in labels:
        labels.append(ABANDONED_LABEL)
    github.request(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        {"state": "closed", "state_reason": "not_planned", "labels": labels},
    )
    # Re-read: the label is what stops the gate, so confirm it survived.
    current, _ = github.request("GET", f"/repos/{repo}/issues/{number}")
    names = {label["name"] for label in current.get("labels", [])}
    if ABANDONED_LABEL not in names:
        github.request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            {"labels": [ABANDONED_LABEL]},
        )


def kept_issue(promotion_dir: Path, issue_file: Path) -> Issue | None:
    """Return the issue the put posted, or None when it posted nothing.

    Raises ValueError when the put should have posted one but names none:
    superseding everything then would close the issue that approves the
    release.
    """
    if (promotion_dir / "skip").exists():
        return None
    posted = json.loads(issue_file.read_text())
    number = int(posted.get("issue_number") or 0)
    if number <= 0:
        msg = f"the release issue put reported no issue in {issue_file}"
        raise ValueError(msg)
    return {"number": number, "title": posted.get("issue_title", "")}


def main() -> None:
    """Supersede every open gate issue other than the one just posted."""
    app = os.environ["APP_NAME"]
    repo = os.environ["GITHUB_REPOSITORY"]
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    promotion_dir = Path(os.environ["PROMOTION_DIR"])
    try:
        keep = kept_issue(promotion_dir, Path(os.environ["ISSUE_FILE"]))
    except (ValueError, OSError) as error:
        sys.exit(f"Refusing to supersede {app}'s gate issues: {error}.")

    token = installation_token(
        api_url,
        os.environ["GITHUB_APP_ID"],
        os.environ["GITHUB_APP_INSTALLATION_ID"],
        os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    github = GitHub(api_url, token)
    stale = stale_issues(
        app, github.open_issues(repo), keep["number"] if keep else None
    )
    if not stale:
        print(f"{app}: no stale gate issues.")  # noqa: T201
        return

    comment = comment_for(
        keep,
        "Production already matches this code: no release is waiting and the "
        "Production preview shows no changes.",
    )
    for issue in stale:
        print(f"{app}: superseding #{issue['number']} ({issue['title']!r}).")  # noqa: T201
        supersede(github, repo, issue, comment)


if __name__ == "__main__":
    main()
