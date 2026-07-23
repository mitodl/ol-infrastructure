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


def load_repos_config() -> dict[str, AppConfig]:
    raw = json.loads(os.environ["REPOS_CONFIG"])
    return {name: AppConfig(**cfg) for name, cfg in raw.items()}
