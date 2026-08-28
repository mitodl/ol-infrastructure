"""Tests for the crawl-cache schema guard in `bin/github-org-inventory`.

Every command in that file runs off one cached crawl, the cache carries no version, and
the repo records are read by subscript in about forty places. So a cache written before
a field existed loads cleanly and then dies with a bare `KeyError` several hundred lines
away, naming a key rather than the cache that lacks it. That is how it was reported on
#5566, against `foreign_required_status_checks`, and the same trap re-arms itself every
time somebody adds a field.

The guard turns that into one message that names the remedy. `--refresh` was always the
answer; the point is that the failure says so.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "bin" / "github-org-inventory"


def _load() -> Any:
    """Import the CLI from a file with no `.py` on it, without running a crawl."""
    loader = SourceFileLoader("github_org_inventory", str(SCRIPT))
    spec = importlib.util.spec_from_file_location(loader.name, SCRIPT, loader=loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load {SCRIPT}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


def _cache(tmp_path: Path, repos: list[dict[str, Any]]) -> Path:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"repos": repos}))
    return path


def _current(**overrides: Any) -> dict[str, Any]:
    record = dict.fromkeys(tool._EXPECTED_REPO_KEYS, "x")
    return record | overrides


def test_a_current_cache_loads(tmp_path: Path) -> None:
    """The ordinary path: a cache written by this code is used as-is."""
    cache = _cache(tmp_path, [_current(name="a"), _current(name="b")])
    assert tool._crawl_org(cache, refresh=False)["repos"][0]["name"] == "a"


def test_the_reported_failure_is_named_not_raised_as_a_keyerror(
    tmp_path: Path,
) -> None:
    """The #5566 case: a cache from before this PR added the two check fields."""
    stale = _current(name="a")
    del stale["required_status_checks"]
    del stale["foreign_required_status_checks"]
    with pytest.raises(SystemExit) as exited:
        tool._crawl_org(_cache(tmp_path, [stale]), refresh=False)
    message = str(exited.value)
    assert "required_status_checks" in message
    assert "foreign_required_status_checks" in message
    assert "--refresh" in message


def test_one_short_record_is_enough_to_fail(tmp_path: Path) -> None:
    """A half-written cache reads exactly like one written by older code.

    Checking only the first record would clear this, and the subscript that dies is not
    guaranteed to land on the record that is missing the key.
    """
    short = _current(name="b")
    del short["ruleset_count"]
    with pytest.raises(SystemExit, match="ruleset_count"):
        tool._crawl_org(_cache(tmp_path, [_current(name="a"), short]), refresh=False)


def test_an_empty_crawl_is_not_treated_as_stale(tmp_path: Path) -> None:
    """No records means no evidence of an old schema, and nothing to subscript."""
    assert tool._crawl_org(_cache(tmp_path, []), refresh=False) == {"repos": []}


class _ReachedCrawlError(Exception):
    """Proves control reached the live crawl instead of stopping at the cache guard."""


def test_refresh_does_not_consult_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--refresh` must reach the crawl even when the cache on disk is stale.

    Otherwise the guard would block the one command that fixes what it complains about.
    """

    def _reached(_token: str) -> None:
        raise _ReachedCrawlError

    monkeypatch.setattr(tool, "get_installation_token", lambda: "t")
    monkeypatch.setattr(tool, "_client", _reached)
    stale = _current(name="a")
    del stale["required_status_checks"]
    with pytest.raises(_ReachedCrawlError):
        tool._crawl_org(_cache(tmp_path, [stale]), refresh=True)
