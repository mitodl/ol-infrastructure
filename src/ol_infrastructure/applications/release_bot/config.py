"""Configuration loader for the release bot."""

import json
import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    pipeline: str
    repo: str    # "mitodl/mit-learn"
    branch: str  # "main" or "master"


def load_repos_config() -> dict[str, AppConfig]:
    raw = json.loads(os.environ["REPOS_CONFIG"])
    return {name: AppConfig(**cfg) for name, cfg in raw.items()}
