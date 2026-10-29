"""Configuration loader for the release bot."""

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class AppConfig:
    pipeline: str
    repo: str  # "mitodl/mit-learn"
    branch: str  # "main" or "master"
    # Slack channel ID for proactive (non-slash-command) notifications, e.g.
    # "ready to promote". Falls back to RELEASE_ANNOUNCE_CHANNEL if unset.
    channel: str | None = None
    # Whether the app's Concourse pipeline is the modernized release-resource
    # shape. Mirrors AppRegistration.release_resource_workflow in
    # bridge.settings.apps, which __main__.py copies into REPOS_CONFIG. False
    # means every release command the bot offers would target a job or an
    # artifact that app's pipeline does not have, so the handlers refuse
    # instead. Defaults False: an app absent from a hand-written repos_config
    # override gets the refusal, which says what is wrong, rather than an
    # opaque Concourse 404.
    release_workflow: bool = False


def release_workflow_apps(repos: dict[str, AppConfig]) -> dict[str, AppConfig]:
    """Return only the apps whose pipeline the bot can actually drive."""
    return {name: cfg for name, cfg in repos.items() if cfg.release_workflow}


@dataclass
class LibraryConfig:
    """One publishable library. Mirrors bridge.settings.libraries.

    A library is published, not released: no RC/Production split, no release
    issue, no checklist. Exactly one of `publish_job` and `package_job_prefix`
    is set -- the latter for a monorepo, whose packages are read from the
    pipeline's live job list rather than recorded here.
    """

    pipeline: str
    team: str
    publish_job: str | None = None
    package_job_prefix: str | None = None
    github_repo: str | None = None
    registry: str = "PyPI"


def load_repos_config() -> dict[str, AppConfig]:
    raw = json.loads(os.environ["REPOS_CONFIG"])
    return {name: AppConfig(**cfg) for name, cfg in raw.items()}


def load_libraries_config() -> dict[str, LibraryConfig]:
    """Load the publishable-library registry.

    Optional, unlike REPOS_CONFIG: a bot image newer than its Pulumi stack
    starts with no libraries and says so, rather than crashing on boot and
    taking every other command down with it.
    """
    raw = json.loads(os.environ.get("LIBRARIES_CONFIG", "{}"))
    return {name: LibraryConfig(**cfg) for name, cfg in raw.items()}


def load_unpublishable_libraries() -> dict[str, str]:
    """Load name -> why-it-has-no-pipeline, for libraries Doof could publish."""
    return json.loads(os.environ.get("UNPUBLISHABLE_LIBRARIES", "{}"))


def load_slack_user_overrides() -> dict[str, str]:
    """Load commit-identity -> Slack member id overrides for author mentions.

    Maps a commit email or GitHub handle to the Slack account to @-mention,
    for the authors whose commit email is not their Slack email and so cannot
    be found with ``users.lookupByEmail`` (see slack_users.py). Sourced from
    the SOPS-encrypted release_bot secret rather than bridge.settings so a
    roster of people is not published in a public repository.

    Optional and non-fatal in both directions: an absent variable just means
    no overrides, and malformed JSON degrades to no overrides with a log line.
    Both leave every author resolving exactly as before this existed, which
    beats a crash loop that takes every other command down with it.
    """
    raw = os.environ.get("SLACK_USER_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        overrides = json.loads(raw)
    except ValueError:
        log.exception(
            "SLACK_USER_OVERRIDES is not valid JSON; release notifications "
            "will fall back to naming authors without @-mentioning them"
        )
        return {}
    if not isinstance(overrides, dict):
        log.error(
            "SLACK_USER_OVERRIDES must be a JSON object of identity -> Slack "
            "member id, got %s",
            type(overrides).__name__,
        )
        return {}
    return {str(key): str(value) for key, value in overrides.items()}
