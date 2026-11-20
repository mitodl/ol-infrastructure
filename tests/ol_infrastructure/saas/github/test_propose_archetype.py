"""`propose_archetype` keeps detached forks out of the `fork` bucket.

`infer-archetypes` rewrites the assignment file from this heuristic, so a GitHub fork we
reclassified by hand would otherwise drop back to `fork` / `unmanaged` on the next run.
"""

from __future__ import annotations

from typing import Any

import yaml

from tests.ol_infrastructure.saas.github.test_crawl_cache import SCRIPT, _load

INVENTORY = _load()


def _repo(name: str, *, fork: bool) -> dict[str, Any]:
    return {
        "name": name,
        "archived": False,
        "fork": fork,
        "topics": [],
        "environment_names": ["copilot", "pypi"],
    }


def test_detached_fork_is_a_library() -> None:
    assert INVENTORY.DETACHED_FORKS
    for name in INVENTORY.DETACHED_FORKS:
        assert INVENTORY.propose_archetype(_repo(name, fork=True)) == "library"


def test_other_forks_stay_forks() -> None:
    assert INVENTORY.propose_archetype(_repo("some-upstream", fork=True)) == "fork"


def test_detached_forks_are_assigned_library() -> None:
    # DATA_DIR is relative to the repo root; resolve it so pytest can run from anywhere.
    assignments = yaml.safe_load(
        (
            SCRIPT.parents[1] / INVENTORY.DATA_DIR / "archetypes-proposed.yaml"
        ).read_text()
    )
    assert set(assignments["library"]) >= INVENTORY.DETACHED_FORKS
