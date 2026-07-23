"""GitHub API client for the release bot."""

import os
import re
from typing import Any

import aiohttp

GITHUB_API = "https://api.github.com"

_RELEASE_TAG_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
_CHECKLIST_LINE_RE = re.compile(r"^- \[( |x)\]")
# Matches the "## Release <version>" header the release resource's
# _build_checklist() writes at the top of the issue body. The issue *title*
# is always just "Release {app_name}" with no version -- the version only
# ever appears here.
_VERSION_HEADER_RE = re.compile(r"^## Release (?P<version>\S+)", re.MULTILINE)

# Applied once a release issue's checklist is fully checked, so subsequent
# polls don't re-notify Slack every cycle. Not a gate signal itself -- purely
# a "have we already posted about this" marker for the bot.
PROMOTE_READY_LABEL = "promote-ready"


def _github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        msg = "GITHUB_TOKEN environment variable must be set"
        raise RuntimeError(msg)
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_github_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _latest_release_tag(
    session: aiohttp.ClientSession, repo_slug: str
) -> str | None:
    """Return the most recent YYYY.MM.DD.N tag, or None if none exists."""
    url = f"{GITHUB_API}/repos/{repo_slug}/tags"
    async with session.get(
        url, headers=_auth_headers(), params={"per_page": "100"}
    ) as resp:
        resp.raise_for_status()
        tags = await resp.json()

    for tag in tags:
        if _RELEASE_TAG_RE.match(tag["name"]):
            return tag["name"]
    return None


async def commits_since_last_tag(repo_slug: str) -> list[dict[str, Any]]:
    """Return commits on the default branch since the most recent YYYY.MM.DD.N tag.

    Uses the compare API (/compare/{tag}...{branch}) so only commits *after*
    the tag are returned — the GET /commits?sha= endpoint returns history
    *starting from* that SHA (i.e. ancestors), which is the opposite of what
    we want.
    """
    async with aiohttp.ClientSession() as session:
        latest_tag = await _latest_release_tag(session, repo_slug)

        if latest_tag:
            # Resolve the default branch name for this repo
            repo_url = f"{GITHUB_API}/repos/{repo_slug}"
            async with session.get(repo_url, headers=_auth_headers()) as resp:
                resp.raise_for_status()
                repo_data = await resp.json()
            default_branch = repo_data["default_branch"]

            compare_url = (
                f"{GITHUB_API}/repos/{repo_slug}/compare/"
                f"{latest_tag}...{default_branch}"
            )
            async with session.get(compare_url, headers=_auth_headers()) as resp:
                resp.raise_for_status()
                compare_data = await resp.json()
            # "diverged" status means the tag is not an ancestor of the branch;
            # the response omits "commits" entirely in that case.
            raw_commits = compare_data.get("commits", [])
        else:
            url = f"{GITHUB_API}/repos/{repo_slug}/commits"
            async with session.get(
                url, headers=_auth_headers(), params={"per_page": "50"}
            ) as resp:
                resp.raise_for_status()
                raw_commits = await resp.json()

    return [
        {
            "sha": c["sha"],
            "message": c["commit"]["message"],
            "author": c["commit"]["author"]["name"],
            "url": c["html_url"],
        }
        for c in raw_commits
    ]


async def open_release_issues(repo_slug: str) -> list[dict[str, Any]]:
    """Return open GitHub Issues labelled 'release'."""
    url = f"{GITHUB_API}/repos/{repo_slug}/issues"
    params = {"state": "open", "labels": "release", "per_page": "10"}
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, headers=_auth_headers(), params=params) as resp,
    ):
        resp.raise_for_status()
        issues = await resp.json()

    return [
        {
            "number": i["number"],
            "title": i["title"],
            "url": i["html_url"],
            "body": i.get("body") or "",
            "labels": [lbl["name"] for lbl in i.get("labels", [])],
        }
        for i in issues
    ]


def checklist_status(body: str) -> tuple[int, int]:
    """Return (checked_count, total_count) of checklist lines in an issue body."""
    total = 0
    checked = 0
    for line in body.splitlines():
        match = _CHECKLIST_LINE_RE.match(line)
        if match:
            total += 1
            if match.group(1) == "x":
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


async def add_issue_label(repo_slug: str, issue_number: int, label: str) -> None:
    """Add a label to the given issue. Idempotent -- GitHub dedupes existing labels."""
    url = f"{GITHUB_API}/repos/{repo_slug}/issues/{issue_number}/labels"
    async with (
        aiohttp.ClientSession() as session,
        session.post(url, headers=_auth_headers(), json={"labels": [label]}) as resp,
    ):
        resp.raise_for_status()


async def close_release_issue(repo_slug: str, issue_number: int, comment: str) -> None:
    """Add a comment and close the given issue (triggers Concourse production deploy).

    Closing the release issue is the entire production promotion mechanism —
    Concourse's github-issues resource polls for closed issues.
    """
    async with aiohttp.ClientSession() as session:
        comment_url = f"{GITHUB_API}/repos/{repo_slug}/issues/{issue_number}/comments"
        async with session.post(
            comment_url,
            headers=_auth_headers(),
            json={"body": comment},
        ) as resp:
            resp.raise_for_status()

        close_url = f"{GITHUB_API}/repos/{repo_slug}/issues/{issue_number}"
        async with session.patch(
            close_url,
            headers=_auth_headers(),
            json={"state": "closed"},
        ) as resp:
            resp.raise_for_status()
