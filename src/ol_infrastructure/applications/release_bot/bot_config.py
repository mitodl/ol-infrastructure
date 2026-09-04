"""Configuration loader for the release bot."""

import json
import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    pipeline: str
    repo: str  # "mitodl/mit-learn"
    branch: str  # "main" or "master"
    # Slack channel ID for proactive (non-slash-command) notifications, e.g.
    # "ready to promote". Falls back to RELEASE_ANNOUNCE_CHANNEL if unset.
    channel: str | None = None


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
