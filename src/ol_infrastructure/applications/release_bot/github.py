"""GitHub API client for the release bot."""

import os
import re

import aiohttp

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"

_RELEASE_TAG_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _latest_release_tag(session: aiohttp.ClientSession, repo_slug: str) -> str | None:
    """Return the most recent YYYY.MM.DD.N tag, or None if none exists."""
    url = f"{GITHUB_API}/repos/{repo_slug}/tags"
    async with session.get(url, headers=_auth_headers(), params={"per_page": "100"}) as resp:
        resp.raise_for_status()
        tags = await resp.json()

    for tag in tags:
        if _RELEASE_TAG_RE.match(tag["name"]):
            return tag["name"]
    return None


async def commits_since_last_tag(repo_slug: str) -> list[dict]:
    """Return commits on the default branch since the most recent YYYY.MM.DD.N tag."""
    async with aiohttp.ClientSession() as session:
        latest_tag = await _latest_release_tag(session, repo_slug)

        params: dict[str, str] = {"per_page": "50"}
        if latest_tag:
            # Get the commit SHA for the tag so we can use `since`
            url = f"{GITHUB_API}/repos/{repo_slug}/git/refs/tags/{latest_tag}"
            async with session.get(url, headers=_auth_headers()) as resp:
                resp.raise_for_status()
                ref_data = await resp.json()
            tag_sha = ref_data["object"]["sha"]

            # If the tag points to a tag object (annotated tag), resolve to commit SHA
            if ref_data["object"]["type"] == "tag":
                tag_url = ref_data["object"]["url"]
                async with session.get(tag_url, headers=_auth_headers()) as resp:
                    resp.raise_for_status()
                    tag_obj = await resp.json()
                tag_sha = tag_obj["object"]["sha"]

            params["sha"] = tag_sha

        url = f"{GITHUB_API}/repos/{repo_slug}/commits"
        async with session.get(url, headers=_auth_headers(), params=params) as resp:
            resp.raise_for_status()
            raw_commits = await resp.json()

    # Exclude the tag commit itself when using a base SHA
    if latest_tag and raw_commits:
        tag_sha_prefix = params.get("sha", "")
        raw_commits = [c for c in raw_commits if not c["sha"].startswith(tag_sha_prefix)]

    return [
        {
            "sha": c["sha"],
            "message": c["commit"]["message"],
            "author": c["commit"]["author"]["name"],
            "url": c["html_url"],
        }
        for c in raw_commits
    ]


async def open_release_issues(repo_slug: str) -> list[dict]:
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
        }
        for i in issues
    ]


async def close_release_issue(repo_slug: str, issue_number: int, comment: str) -> None:
    """Add a comment and close the given issue (triggers Concourse production deploy)."""
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
