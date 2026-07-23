"""Canonical per-application registry: repo, default branch, and notification identity.

Consumed by both the Concourse pipeline generator
(``src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py``) and the
release bot (``src/ol_infrastructure/applications/release_bot/__main__.py``),
so an app's GitHub repo, default branch, and Slack channel are defined in
exactly one place instead of being hand-duplicated (and drifting) across
both control surfaces.

``APPS`` is the canonical enumeration of every app both control surfaces
know about -- the release bot iterates its keys directly
(``for app_name in APPS``) to build its config, so an app must have an entry
here to be picked up at all, even if every field is left at its default.
Only the *field values* default sparsely: leave ``github_repo``/
``repo_main_branch`` unset on an ``AppRegistration()`` when the app's repo is
``mitodl/{app_name}`` on branch ``"main"``; the accessor functions below fill
those in.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppRegistration:
    """Canonical metadata for one deployable application.

    :param github_repo: "owner/repo" slug. Defaults to ``mitodl/{app_name}``
        when unset -- only set this when the repo name differs from the app
        name (e.g. app ``xpro`` lives in repo ``mitodl/mitxpro``).
    :param repo_main_branch: The repo's default branch.
    :param slack_channel: Slack channel ID for release-bot notifications
        (e.g. "ready to promote"). Unset means notifications are skipped for
        this app unless RELEASE_ANNOUNCE_CHANNEL provides a fallback.
    """

    github_repo: str | None = None
    repo_main_branch: str = "main"
    slack_channel: str | None = None


APPS: dict[str, AppRegistration] = {
    "learn-ai": AppRegistration(),
    "micromasters": AppRegistration(repo_main_branch="master"),
    "mit-learn": AppRegistration(),
    "mit-learn-nextjs": AppRegistration(github_repo="mitodl/mit-learn"),
    "mitxonline": AppRegistration(),
    "ocw-studio": AppRegistration(repo_main_branch="master"),
    "odl-video-service": AppRegistration(repo_main_branch="master"),
    "ol-analytics-api": AppRegistration(),
    "xpro": AppRegistration(github_repo="mitodl/mitxpro", repo_main_branch="master"),
}


def github_repo(app_name: str) -> str:
    """Return the "owner/repo" slug for the given app."""
    entry = APPS.get(app_name)
    if entry and entry.github_repo:
        return entry.github_repo
    return f"mitodl/{app_name}"


def repo_main_branch(app_name: str) -> str:
    """Return the default branch of the given app's repo."""
    entry = APPS.get(app_name)
    return entry.repo_main_branch if entry else "main"


def slack_channel(app_name: str) -> str | None:
    """Return the configured Slack channel ID for release notifications, if any."""
    entry = APPS.get(app_name)
    return entry.slack_channel if entry else None
