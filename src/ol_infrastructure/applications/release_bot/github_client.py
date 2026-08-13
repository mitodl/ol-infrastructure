"""GitHub API client for the release bot, backed by PyGithub.

Authenticates as a GitHub App installation only -- no PAT fallback. The App
fields (GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID / GITHUB_APP_PRIVATE_KEY)
line up with the same App credentials Concourse's `github-issues` resource
type accepts via `auth_method="app"` (see
`ol_concourse.lib.resources.github_issues`), so both consumers are pointed at
one GitHub App installation instead of each holding a separate long-lived
PAT. PyGithub's `AppInstallationAuth` mints and refreshes installation tokens
on its own, so no manual token-refresh bookkeeping is needed here.
"""

import asyncio
import itertools
import logging
import os
import re
from datetime import UTC, date, datetime
from typing import Any

from github import Auth, Github

log = logging.getLogger(__name__)

# Month and day are NOT zero-padded: the release resource emits PEP 440
# calver (2026.8.3.1, not 2026.08.03.1) because uv and other modern Python
# tooling reject leading zeros. This pattern required \d{2} for both, so it
# matched no tag any current release has ever produced -- _latest_release_tag()
# returned None and release notes silently degraded to "the last 50 commits of
# all history". The old fixtures hid it by testing with padded tags.
_RELEASE_TAG_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})\.(\d+)$")
# Commits the release resource authors for a release, excluded from release
# notes so a finished release does not read as unreleased work.
_RELEASE_MACHINERY_RE = re.compile(
    r"^(Release|Merge releases/)\s*\d{4}\.\d{1,2}\.\d{1,2}\.\d+$"
)
_RELEASE_BRANCH_PREFIX = "releases/"
_CHECKLIST_LINE_RE = re.compile(r"^- \[( |x)\]", re.IGNORECASE)
# A checklist item is "- [ ] <description> by <author>". The description can
# itself contain " by ", so the greedy .* deliberately binds to the *last*
# occurrence, which is the one the release resource wrote.
_UNCHECKED_ITEM_RE = re.compile(r"^- \[ \]\s+(?P<item>.*)\s+by\s+(?P<author>\S+)\s*$")
# Matches the "## Release <version>" header the release resource's
# _build_checklist() writes at the top of the issue body. The issue *title*
# is always just "Release {app_name}" with no version -- the version only
# ever appears here.
_VERSION_HEADER_RE = re.compile(r"^## Release (?P<version>\S+)", re.MULTILINE)

# Applied once a release issue's checklist is fully checked, so subsequent
# polls don't re-notify Slack every cycle. Not a gate signal itself -- purely
# a "have we already posted about this" marker for the bot.
PROMOTE_READY_LABEL = "promote-ready"

# Cap on tags scanned when picking the release baseline. Selection sorts by
# parsed version rather than trusting GitHub's tag ordering, so this must cover
# *all* release tags to be correct -- truncating an unordered listing can hide
# the highest tag and silently restore the stale-baseline bug. Set high enough
# that release repositories never reach it; hitting it is logged, not silent.
_TAG_SCAN_LIMIT = 5000
_COMMIT_LIST_LIMIT = 50

_client: Github | None = None


def _build_auth() -> Auth.Auth:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not (app_id and installation_id and private_key):
        msg = (
            "GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, and "
            "GITHUB_APP_PRIVATE_KEY must all be set"
        )
        raise RuntimeError(msg)
    try:
        app_id_int = int(app_id)
        installation_id_int = int(installation_id)
    except ValueError as exc:
        msg = (
            "GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID must be numeric "
            f"(got GITHUB_APP_ID={app_id!r}, "
            f"GITHUB_APP_INSTALLATION_ID={installation_id!r})"
        )
        raise RuntimeError(msg) from exc
    return Auth.AppAuth(app_id_int, private_key).get_installation_auth(
        installation_id_int
    )


def _get_client() -> Github:
    global _client  # noqa: PLW0603
    if _client is None:
        base_url = os.environ.get("GITHUB_API_BASE_URL", "").strip()
        if base_url:
            _client = Github(auth=_build_auth(), base_url=base_url)
        else:
            _client = Github(auth=_build_auth())
    return _client


def release_tag_sort_key(tag: str) -> tuple[int, int, int, int]:
    """Return a sortable tuple for a YYYY.M.D.N tag, or zeros if unparseable."""
    match = _RELEASE_TAG_RE.match(tag)
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _release_tags(repo: Any) -> list[str]:
    """Return every YYYY.M.D.N tag on *repo*, up to `_TAG_SCAN_LIMIT`.

    Ordering from `get_tags()` is not documented to be chronological, so the
    whole listing has to be considered before picking a maximum -- a truncated
    scan can omit the highest tag and reinstate the stale baseline this is
    meant to fix.
    """
    scanned = 0
    tags = []
    for tag in itertools.islice(repo.get_tags(), _TAG_SCAN_LIMIT):
        scanned += 1
        if _RELEASE_TAG_RE.match(tag.name):
            tags.append(tag.name)
    if scanned == _TAG_SCAN_LIMIT:
        log.warning(
            "Stopped scanning tags at the %s-tag cap; the release baseline may "
            "be wrong because GitHub's tag order is not version-ordered",
            _TAG_SCAN_LIMIT,
        )
    return tags


def _latest_release_tag(repo: Any) -> str | None:
    """Return the highest YYYY.M.D.N tag, or None if none exists."""
    tags = _release_tags(repo)
    if not tags:
        return None
    return max(tags, key=release_tag_sort_key)


def next_release_version(tags: list[str], today: date) -> str:
    """Return the next YYYY.M.D.N version for *today*.

    Mirrors the release resource's own computation so a preview can report the
    version a release *would* get without triggering anything. Month and day
    are unpadded for PEP 440.
    """
    max_n = 0
    for tag in tags:
        match = _RELEASE_TAG_RE.match(tag)
        if match and tuple(int(p) for p in match.groups()[:3]) == (
            today.year,
            today.month,
            today.day,
        ):
            max_n = max(max_n, int(match.group(4)))
    return f"{today.year}.{today.month}.{today.day}.{max_n + 1}"


def _is_release_machinery(message: str) -> bool:
    """Return True for the commits the release resource itself authors."""
    return bool(_RELEASE_MACHINERY_RE.match(message.splitlines()[0].strip()))


def _commits_since_last_tag_sync(repo_slug: str) -> list[dict[str, Any]]:
    repo = _get_client().get_repo(repo_slug)
    latest_tag = _latest_release_tag(repo)

    if latest_tag:
        comparison = repo.compare(latest_tag, repo.default_branch)
        # "diverged" status means the tag is not an ancestor of the branch;
        # GitHub omits commits entirely in that case.
        raw_commits = (
            []
            if comparison.status == "diverged"
            else itertools.islice(comparison.commits, _COMMIT_LIST_LIMIT)
        )
    else:
        raw_commits = itertools.islice(repo.get_commits(), _COMMIT_LIST_LIMIT)

    return [
        {
            "sha": c.sha,
            "message": c.commit.message,
            "author": c.commit.author.name,
            "url": c.html_url,
        }
        for c in raw_commits
        # "Release X" / "Merge releases/X" land on the default branch *after*
        # the tag (which sits on the pre-bump HEAD), so without this every
        # finished release reads as two commits waiting to be released.
        if not _is_release_machinery(c.commit.message)
    ]


def _in_flight_release_sync(repo_slug: str) -> dict[str, Any] | None:
    """Return the release that was cut but has not yet shipped, or None.

    A release is in flight while its ``releases/YYYY.M.D.N`` branch exists --
    the release resource creates it on ``action: create`` and deletes it on
    ``action: finish``/``abandon``. A branch that outlives its production
    deploy means the finish step never completed.
    """
    repo = _get_client().get_repo(repo_slug)
    versions = []
    for branch in repo.get_branches():
        if not branch.name.startswith(_RELEASE_BRANCH_PREFIX):
            continue
        version = branch.name[len(_RELEASE_BRANCH_PREFIX) :]
        if _RELEASE_TAG_RE.match(version):
            versions.append((version, branch))
    if not versions:
        return None
    version, branch = max(versions, key=lambda pair: release_tag_sort_key(pair[0]))
    return {
        "version": version,
        "branch": branch.name,
        "url": f"https://github.com/{repo_slug}/tree/{branch.name}",
        "cut_at": _branch_commit_date(branch),
    }


def _branch_commit_date(branch: Any) -> datetime | None:
    """Return the branch tip's authored date, or None if unavailable.

    Reaching through to the commit can cost an extra API call and is only ever
    used to say "cut N days ago", so a failure here must not take down the
    in-flight report it decorates.
    """
    try:
        return branch.commit.commit.author.date
    except Exception:  # noqa: BLE001
        log.debug("Could not read commit date for branch %s", branch.name)
        return None


def _release_preview_sync(repo_slug: str) -> dict[str, Any]:
    """Return what the next release would contain, without creating anything."""
    repo = _get_client().get_repo(repo_slug)
    tags = _release_tags(repo)
    latest_tag = max(tags, key=release_tag_sort_key) if tags else None
    return {
        "version": next_release_version(tags, datetime.now(tz=UTC).date()),
        "since": latest_tag,
        "commits": _commits_since_last_tag_sync(repo_slug),
        "in_flight": _in_flight_release_sync(repo_slug),
    }


async def in_flight_release(repo_slug: str) -> dict[str, Any] | None:
    """Return the cut-but-unshipped release for *repo_slug*, or None."""
    return await asyncio.to_thread(_in_flight_release_sync, repo_slug)


async def release_preview(repo_slug: str) -> dict[str, Any]:
    """Return the next version, its commits, and any in-flight release."""
    return await asyncio.to_thread(_release_preview_sync, repo_slug)


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


def unchecked_authors(body: str) -> set[str]:
    """Return the authors who still have unchecked items in a checklist body.

    The release resource writes each item as "- [ ] <description> by
    <author>", where <author> is the commit author's email. This is what makes
    "still waiting on ..." possible instead of a bare unchecked count.
    """
    authors = set()
    for line in body.splitlines():
        match = _UNCHECKED_ITEM_RE.match(line.rstrip())
        if match:
            authors.add(match.group("author"))
    return authors


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
