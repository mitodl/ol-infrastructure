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
# matched no tag any current release has ever produced -- the baseline lookup
# found no calver tag and release notes silently degraded to "the last 50
# commits of all history". The old fixtures hid it by testing with padded tags.
_RELEASE_TAG_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})\.(\d+)$")
# The legacy release-script's tags (v0.37.3), which the release resource falls
# back to as a baseline until an app's first calver release. Same pattern as
# the resource's SEMVER_PATTERN.
_SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# Commits the release resource authors for a release, excluded from release
# notes so a finished release does not read as unreleased work.
_RELEASE_MACHINERY_RE = re.compile(
    r"^(Release|Merge releases/)\s*\d{4}\.\d{1,2}\.\d{1,2}\.\d+$"
)
_RELEASE_BRANCH_PREFIX = "releases/"
# The two ways a merged PR names itself on the default branch: a squash merge
# appends " (#123)" to the PR title, and a merge commit's subject is
# "Merge pull request #123 from <owner>/<branch>" with the PR title as the
# first body line. Release notes need the number so they can link the PR --
# the release resource fills commits.json from the PR API instead, one call
# per commit, which is more than a Slack reply should cost.
_SQUASH_PR_RE = re.compile(r"\s*\(#(?P<number>\d+)\)$")
_MERGE_PR_RE = re.compile(r"^Merge pull request #(?P<number>\d+) from \S+$")
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


def _semver_sort_key(tag: str) -> tuple[int, int, int]:
    """Return a sortable tuple for a vX.Y.Z or X.Y.Z tag, or zeros if unparseable."""
    match = _SEMVER_TAG_RE.match(tag)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _release_tags(repo: Any) -> tuple[list[str], list[str]]:
    """Return *repo*'s YYYY.M.D.N tags and its semver tags, up to `_TAG_SCAN_LIMIT`.

    Ordering from `get_tags()` is not documented to be chronological, so the
    whole listing has to be considered before picking a maximum -- a truncated
    scan can omit the highest tag and reinstate the stale baseline this is
    meant to fix.
    """
    scanned = 0
    tags = []
    semver_tags = []
    for tag in itertools.islice(repo.get_tags(), _TAG_SCAN_LIMIT):
        scanned += 1
        if _RELEASE_TAG_RE.match(tag.name):
            tags.append(tag.name)
        elif _SEMVER_TAG_RE.match(tag.name):
            semver_tags.append(tag.name)
    if scanned == _TAG_SCAN_LIMIT:
        log.warning(
            "Stopped scanning tags at the %s-tag cap; the release baseline may "
            "be wrong because GitHub's tag order is not version-ordered",
            _TAG_SCAN_LIMIT,
        )
    return tags, semver_tags


def _release_baseline(repo: Any) -> str | None:
    """Return the tag the next release is cut against, or None if none exists.

    That is the highest YYYY.M.D.N tag, or before an app's first calver release,
    its highest semver tag. The fallback mirrors the release resource's
    ``semver_tag_fallback``, which k8s_apps sets for every release-resource app.
    Without it a repo moving off the legacy pipeline reads as never released,
    and the preview lists its whole history as the next release.
    """
    tags, semver_tags = _release_tags(repo)
    if tags:
        return max(tags, key=release_tag_sort_key)
    if semver_tags:
        return max(semver_tags, key=_semver_sort_key)
    return None


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
    subject = (message.splitlines() or [""])[0]
    return bool(_RELEASE_MACHINERY_RE.match(subject.strip()))


def pull_request(message: str) -> tuple[int | None, str]:
    """Return the PR number a commit message names, and the PR's title.

    The number is None for a commit that did not come from a PR (or whose
    message does not say so); the title is then the commit subject.
    """
    lines = message.splitlines() or [""]
    subject = lines[0].strip()
    if match := _MERGE_PR_RE.match(subject):
        # The merge subject names only the branch; the PR title, when GitHub
        # wrote one, is the first non-blank body line.
        body = [line.strip() for line in lines[1:] if line.strip()]
        return int(match.group("number")), body[0] if body else subject
    if match := _SQUASH_PR_RE.search(subject):
        return int(match.group("number")), subject[: match.start()]
    return None, subject


def _merged_by_pull_requests(raw_commits: list[Any]) -> set[str]:
    """Return the SHAs a PR merge commit in *raw_commits* brought in.

    A merge commit's PR is listed under the merge commit itself (see
    `pull_request`), but the range also holds the commits it merged, so the
    change would be listed twice: once linked to the PR, once as a bare SHA.
    Those commits are what is reachable from the merge's other parents but
    not from its first, the branch it was merged into. Only commits in the
    list are walked; `parents` comes with each commit, so this costs no API
    calls.

    Only the outermost PR merges count. Whatever a nested PR merge brought in
    that its outer one also hides is hidden already, and the rest is on the
    outer merge's first side: a PR syncing main into a feature branch brings
    in main's own commits, which must stay listed.
    """
    parents = {c.sha: [p.sha for p in c.parents] for c in raw_commits}

    def reachable(starts: list[str]) -> set[str]:
        seen: set[str] = set()
        stack = [sha for sha in starts if sha in parents]
        while stack:
            sha = stack.pop()
            if sha not in seen:
                seen.add(sha)
                stack += [p for p in parents[sha] if p in parents]
        return seen

    brought_in: dict[str, set[str]] = {}
    for commit in raw_commits:
        subject = (commit.commit.message.splitlines() or [""])[0].strip()
        commit_parents = parents[commit.sha]
        # Other merges (a branch merged without a PR) keep their commits: the
        # merge's own subject says nothing about what they contain.
        if len(commit_parents) > 1 and _MERGE_PR_RE.match(subject):
            brought_in[commit.sha] = reachable(commit_parents[1:]) - reachable(
                commit_parents[:1]
            )
    nested = set().union(*brought_in.values())
    return set().union(*(shas for sha, shas in brought_in.items() if sha not in nested))


def _commits_since_last_tag_sync(repo_slug: str) -> dict[str, Any]:
    """Return up to ``_COMMIT_LIST_LIMIT`` unreleased commits, and how many more.

    ``omitted`` counts the commits past the cap that were not listed, so a
    caller can say the list is truncated instead of under-reporting the
    release. It is None when the count is unknown: with no release tag the
    "range" is the whole history, and only the cap's worth is fetched.
    """
    repo = _get_client().get_repo(repo_slug)
    latest_tag = _release_baseline(repo)

    omitted: int | None
    if latest_tag:
        # A "diverged" comparison still lists the branch's commits since the
        # merge base -- which is the case while a hotfix is in flight, since
        # its tag sits on a cherry-pick off main. Treating diverged as empty
        # reported "nothing to release" there with commits waiting on main.
        comparison = repo.compare(latest_tag, repo.default_branch)
        raw_commits = list(itertools.islice(comparison.commits, _COMMIT_LIST_LIMIT))
        # total_commits counts the whole range, so this is exact. Release
        # machinery among the omitted commits is counted too, since filtering
        # it would mean fetching the commits the cap exists to skip.
        omitted = max(comparison.total_commits - len(raw_commits), 0)
    else:
        raw_commits = list(itertools.islice(repo.get_commits(), _COMMIT_LIST_LIMIT))
        omitted = None if len(raw_commits) == _COMMIT_LIST_LIMIT else 0

    merged = _merged_by_pull_requests(raw_commits)
    commits = [
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
        if not _is_release_machinery(c.commit.message) and c.sha not in merged
    ]
    return {"commits": commits, "omitted": omitted, "since": latest_tag}


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
    tags, _ = _release_tags(repo)
    unreleased = _commits_since_last_tag_sync(repo_slug)
    return {
        "version": next_release_version(tags, datetime.now(tz=UTC).date()),
        "since": unreleased["since"],
        "commits": unreleased["commits"],
        "omitted": unreleased["omitted"],
        "in_flight": _in_flight_release_sync(repo_slug),
    }


async def in_flight_release(repo_slug: str) -> dict[str, Any] | None:
    """Return the cut-but-unshipped release for *repo_slug*, or None."""
    return await asyncio.to_thread(_in_flight_release_sync, repo_slug)


async def release_preview(repo_slug: str) -> dict[str, Any]:
    """Return the next version, its commits, and any in-flight release."""
    return await asyncio.to_thread(_release_preview_sync, repo_slug)


async def commits_since_last_tag(repo_slug: str) -> dict[str, Any]:
    """Return commits on the default branch since the release baseline.

    The result is ``{"commits": [...], "omitted": int | None, "since": str |
    None}``; see :func:`_commits_since_last_tag_sync` for what ``omitted``
    means and :func:`_release_baseline` for how ``since`` is chosen.

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


# Environments the release pipeline creates GitHub Deployments for (see
# _define_release_resources in
# src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py). Each
# deployment's `ref` is the calver version, because the pipeline starts it
# with `ref: ((.:image_tag))` loaded from the release resource's version file.
RC_ENVIRONMENT = "RC"
PRODUCTION_ENVIRONMENT = "Production"

# How far back to look for a successful deployment. A deployment whose Pulumi
# job failed leaves a non-success status behind and the next release's
# deployment is created above it, so the newest *successful* one can sit a few
# entries down rather than always being first.
_DEPLOYMENT_SCAN_LIMIT = 10
# Release issues are created one per release, newest first, so the issue for a
# just-deployed version is always near the top of the listing.
_RELEASE_ISSUE_SCAN_LIMIT = 30


def _deployment_report_sync(repo_slug: str, environment: str) -> dict[str, Any]:
    repo = _get_client().get_repo(repo_slug)
    deployments = itertools.islice(
        repo.get_deployments(environment=environment), _DEPLOYMENT_SCAN_LIMIT
    )
    success = None
    newer = None
    for index, deployment in enumerate(deployments):
        # get_statuses() is newest-first, and only the newest counts: a
        # deployment that failed and was re-run to success carries both.
        latest_status = next(iter(itertools.islice(deployment.get_statuses(), 1)), None)
        if latest_status is not None and latest_status.state == "success":
            success = {
                "id": deployment.id,
                "version": deployment.ref,
                "sha": deployment.sha,
                "environment": deployment.environment,
                "deployed_at": latest_status.created_at,
                # The github-deployments resource points this at the Concourse
                # build that finished the deployment, when it supplies one.
                "url": latest_status.target_url or "",
            }
            break
        if index == 0:
            # The newest attempt, when it is not itself the newest success:
            # still running, or a job that failed after (or before) its
            # Pulumi deploy changed anything.
            newer = {
                "version": deployment.ref,
                "sha": deployment.sha,
                "created_at": deployment.created_at,
                "state": latest_status.state if latest_status else "no status",
            }
    return {"success": success, "newer": newer}


def _latest_successful_deployment_sync(
    repo_slug: str, environment: str
) -> dict[str, Any] | None:
    return _deployment_report_sync(repo_slug, environment)["success"]


async def deployment_report(repo_slug: str, environment: str) -> dict[str, Any]:
    """Return the newest successful Deployment and any newer unsuccessful one.

    ``{"success": <as latest_successful_deployment> | None, "newer": {version,
    sha, created_at, state} | None}``. ``newer`` is set when the newest
    Deployment in *environment* is not the successful one; the success is
    searched for across the same ``_DEPLOYMENT_SCAN_LIMIT`` entries.
    """
    return await asyncio.to_thread(_deployment_report_sync, repo_slug, environment)


async def latest_successful_deployment(
    repo_slug: str, environment: str
) -> dict[str, Any] | None:
    """Return the newest GitHub Deployment in *environment* that reached success."""
    return await asyncio.to_thread(
        _latest_successful_deployment_sync, repo_slug, environment
    )


def _release_issue_for_version_sync(
    repo_slug: str, version: str
) -> dict[str, Any] | None:
    repo = _get_client().get_repo(repo_slug)
    # state="all": the production milestone fires *after* the release issue was
    # closed to open the promotion gate, so an open-only search would find
    # nothing for exactly the announcement that most needs the link.
    issues = repo.get_issues(
        state="all", labels=["release"], sort="created", direction="desc"
    )
    for issue in itertools.islice(issues, _RELEASE_ISSUE_SCAN_LIMIT):
        if extract_version(issue.body or "") == version:
            return {
                "number": issue.number,
                "url": issue.html_url,
                "title": issue.title,
                "body": issue.body or "",
            }
    return None


async def release_issue_for_version(
    repo_slug: str, version: str
) -> dict[str, Any] | None:
    """Return the release issue whose checklist header names *version*, or None."""
    return await asyncio.to_thread(_release_issue_for_version_sync, repo_slug, version)


# A hotfix is requested with a `hotfix/<full sha>` tag. The release resource's
# check offers it as the next version, and its create deletes the tag by that
# exact name, so anything else under this prefix would never be consumed.
HOTFIX_TAG_PREFIX = "hotfix/"


def _resolve_commit_sync(repo_slug: str, ref: str) -> str:
    return _get_client().get_repo(repo_slug).get_commit(ref).sha


async def resolve_commit(repo_slug: str, ref: str) -> str:
    """Return the full SHA of *ref*, a short or full commit SHA, in *repo_slug*."""
    return await asyncio.to_thread(_resolve_commit_sync, repo_slug, ref)


# Only a tag named for a full SHA is a request. The release resource offers no
# other and deletes a request by that exact name, so anything else under the
# prefix would block releases here while never being cut or consumed there.
_HOTFIX_REQUEST_RE = re.compile(r"^refs/tags/hotfix/[0-9a-f]{40}$")


def _hotfix_refs(repo: Any) -> list[Any]:
    return [
        ref
        for ref in repo.get_git_matching_refs(f"tags/{HOTFIX_TAG_PREFIX}")
        if _HOTFIX_REQUEST_RE.match(ref.ref)
    ]


def _hotfix_sha(ref: Any) -> str:
    return ref.ref.removeprefix(f"refs/tags/{HOTFIX_TAG_PREFIX}")


def _pending_hotfix_requests_sync(repo_slug: str) -> list[str]:
    repo = _get_client().get_repo(repo_slug)
    return [_hotfix_sha(ref) for ref in _hotfix_refs(repo)]


async def pending_hotfix_requests(repo_slug: str) -> list[str]:
    """Return the commit SHAs of hotfixes requested but not yet cut."""
    return await asyncio.to_thread(_pending_hotfix_requests_sync, repo_slug)


def _request_hotfix_sync(repo_slug: str, sha: str) -> None:
    repo = _get_client().get_repo(repo_slug)
    repo.create_git_ref(f"refs/tags/{HOTFIX_TAG_PREFIX}{sha}", sha)


async def request_hotfix(repo_slug: str, sha: str) -> None:
    """Request a hotfix of the full commit *sha* for the release resource to cut."""
    await asyncio.to_thread(_request_hotfix_sync, repo_slug, sha)


def _cancel_hotfix_requests_sync(repo_slug: str) -> list[str]:
    repo = _get_client().get_repo(repo_slug)
    cancelled = []
    for ref in _hotfix_refs(repo):
        ref.delete()
        cancelled.append(_hotfix_sha(ref))
    return cancelled


async def cancel_hotfix_requests(repo_slug: str) -> list[str]:
    """Delete every pending hotfix request and return the SHAs cancelled."""
    return await asyncio.to_thread(_cancel_hotfix_requests_sync, repo_slug)
