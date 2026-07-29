"""GitHub API client for the release bot, backed by PyGithub.

Accepts either a personal access token or a GitHub App installation as the
credential source. The App fields (GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID /
GITHUB_APP_PRIVATE_KEY) line up with the same App credentials Concourse's
`github-issues` resource type accepts via `auth_method="app"` (see
`ol_concourse.lib.resources.github_issues`), so both consumers can be pointed
at one GitHub App installation instead of each holding a separate long-lived
PAT. PyGithub's `AppInstallationAuth` mints and refreshes installation tokens
on its own, so no manual token-refresh bookkeeping is needed here.
"""

import asyncio
import itertools
import os
import re
from typing import Any

from github import Auth, Github

_RELEASE_TAG_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
_CHECKLIST_LINE_RE = re.compile(r"^- \[( |x)\]", re.IGNORECASE)
# Matches the "## Release <version>" header the release resource's
# _build_checklist() writes at the top of the issue body. The issue *title*
# is always just "Release {app_name}" with no version -- the version only
# ever appears here.
_VERSION_HEADER_RE = re.compile(r"^## Release (?P<version>\S+)", re.MULTILINE)

# Applied once a release issue's checklist is fully checked, so subsequent
# polls don't re-notify Slack every cycle. Not a gate signal itself -- purely
# a "have we already posted about this" marker for the bot.
PROMOTE_READY_LABEL = "promote-ready"

_TAG_SCAN_LIMIT = 100
_UNTAGGED_COMMIT_LIMIT = 50

_client: Github | None = None


def _build_auth() -> Auth.Auth:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if app_id and installation_id and private_key:
        return Auth.AppAuth(app_id, private_key).get_installation_auth(
            int(installation_id)
        )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        msg = (
            "Either GITHUB_APP_ID/GITHUB_APP_INSTALLATION_ID/"
            "GITHUB_APP_PRIVATE_KEY or GITHUB_TOKEN must be set"
        )
        raise RuntimeError(msg)
    return Auth.Token(token)


def _get_client() -> Github:
    global _client  # noqa: PLW0603
    if _client is None:
        kwargs = {}
        base_url = os.environ.get("GITHUB_API_BASE_URL", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        _client = Github(auth=_build_auth(), **kwargs)
    return _client


def _latest_release_tag(repo: Any) -> str | None:
    """Return the most recent YYYY.MM.DD.N tag, or None if none exists."""
    for tag in itertools.islice(repo.get_tags(), _TAG_SCAN_LIMIT):
        if _RELEASE_TAG_RE.match(tag.name):
            return tag.name
    return None


def _commits_since_last_tag_sync(repo_slug: str) -> list[dict[str, Any]]:
    repo = _get_client().get_repo(repo_slug)
    latest_tag = _latest_release_tag(repo)

    if latest_tag:
        comparison = repo.compare(latest_tag, repo.default_branch)
        # "diverged" status means the tag is not an ancestor of the branch;
        # GitHub omits commits entirely in that case.
        raw_commits = [] if comparison.status == "diverged" else comparison.commits
    else:
        raw_commits = itertools.islice(repo.get_commits(), _UNTAGGED_COMMIT_LIMIT)

    return [
        {
            "sha": c.sha,
            "message": c.commit.message,
            "author": c.commit.author.name,
            "url": c.html_url,
        }
        for c in raw_commits
    ]


async def commits_since_last_tag(repo_slug: str) -> list[dict[str, Any]]:
    """Return commits on the default branch since the most recent YYYY.MM.DD.N tag.

    Uses the compare API (base=tag, head=branch) so only commits *after* the
    tag are returned -- get_commits(sha=...) walks history *starting from*
    that SHA (i.e. ancestors), which is the opposite of what we want.
    """
    return await asyncio.to_thread(_commits_since_last_tag_sync, repo_slug)


def _open_release_issues_sync(repo_slug: str) -> list[dict[str, Any]]:
    repo = _get_client().get_repo(repo_slug)
    issues = repo.get_issues(state="open", labels=["release"])
    return [
        {
            "number": i.number,
            "title": i.title,
            "url": i.html_url,
            "body": i.body or "",
            "labels": [lbl.name for lbl in i.labels],
        }
        for i in itertools.islice(issues, 10)
    ]


async def open_release_issues(repo_slug: str) -> list[dict[str, Any]]:
    """Return open GitHub Issues labelled 'release'."""
    return await asyncio.to_thread(_open_release_issues_sync, repo_slug)


def checklist_status(body: str) -> tuple[int, int]:
    """Return (checked_count, total_count) of checklist lines in an issue body."""
    total = 0
    checked = 0
    for line in body.splitlines():
        match = _CHECKLIST_LINE_RE.match(line)
        if match:
            total += 1
            if match.group(1).lower() == "x":
                checked += 1
    return checked, total


def is_fully_checked(body: str) -> bool:
    """Return True if the body has at least one checklist line and all are checked."""
    checked, total = checklist_status(body)
    return total > 0 and checked == total


def extract_version(body: str) -> str | None:
    """Return the release version from a checklist body's "## Release X" header."""
    match = _VERSION_HEADER_RE.search(body)
    return match.group("version") if match else None


def _add_issue_label_sync(repo_slug: str, issue_number: int, label: str) -> None:
    repo = _get_client().get_repo(repo_slug)
    repo.get_issue(issue_number).add_to_labels(label)


async def add_issue_label(repo_slug: str, issue_number: int, label: str) -> None:
    """Add a label to the given issue. Idempotent -- GitHub dedupes existing labels."""
    await asyncio.to_thread(_add_issue_label_sync, repo_slug, issue_number, label)


def _close_release_issue_sync(repo_slug: str, issue_number: int, comment: str) -> None:
    issue = _get_client().get_repo(repo_slug).get_issue(issue_number)
    issue.create_comment(comment)
    issue.edit(state="closed")


async def close_release_issue(repo_slug: str, issue_number: int, comment: str) -> None:
    """Add a comment and close the given issue (triggers Concourse production deploy).

    Closing the release issue is the entire production promotion mechanism --
    Concourse's github-issues resource polls for closed issues.
    """
    await asyncio.to_thread(_close_release_issue_sync, repo_slug, issue_number, comment)
