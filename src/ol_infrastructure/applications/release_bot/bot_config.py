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


def load_repos_config() -> dict[str, AppConfig]:
    raw = json.loads(os.environ["REPOS_CONFIG"])
    return {name: AppConfig(**cfg) for name, cfg in raw.items()}
