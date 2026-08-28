"""Tests for the scoring rule in `bin/github-required-checks`.

This is the judgement that decides whether a name may be required at all, and getting it
wrong is not a failed test run -- it is every PR on one of the org's busiest repos stuck
on "Expected -- Waiting for status to be reported", with no dry run available to catch
it first (see rulesets.py).

The rule has to separate three things a raw "appeared on N of the last 40 PRs" cannot,
all of them live in this org:

    `Run zizmor`    1/40, because its workflow is path-filtered. NEVER requirable.
    `ci-gate`       31/40 on ol-infrastructure, because it landed in #5567 on
                    2026-08-24 and the misses are branches cut before it existed.
                    Requirable, and the whole point of the SEC-03 work.
    `test`          39/40, because pytest.yml never ran at all on #5590. Requirable;
                    that PR needed a re-trigger under any ruleset.

The first two sit at opposite verdicts with similar counts, so the tests below are
mostly about which PRs are allowed to count as evidence.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[4] / "bin" / "github-required-checks"


def _load() -> Any:
    """Import the CLI from a file with no `.py` on it.

    The loader has to be handed over explicitly: `spec_from_file_location` picks one by
    file extension, and there is not one to pick by here.
    """
    loader = SourceFileLoader("github_required_checks", str(SCRIPT))
    spec = importlib.util.spec_from_file_location(loader.name, SCRIPT, loader=loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load {SCRIPT}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


def _evidence(*, produced: int, eligible: int, filtered: bool = False) -> Any:
    found = tool.Evidence()
    found.produced = produced
    found.eligible = eligible
    found.filtered = filtered
    found.workflow = ".github/workflows/ci.yml"
    return found


def test_clean_over_a_thin_window_is_safe() -> None:
    """`ci-gate` on mitxonline was 6/6 the day it was first required.

    A short window is thin evidence, not bad evidence. Demanding a long one would make
    every newly added job unrequirable for as long as it takes 20 PRs to merge, which is
    the same as never requiring the gate this whole design rests on.
    """
    assert _evidence(produced=6, eligible=6).safe


def test_one_miss_is_not_rounded_up() -> None:
    """39/40 is a check some PR did not produce; requiring it blocks that PR."""
    assert not _evidence(produced=39, eligible=40).safe


def test_never_produced_is_not_safe() -> None:
    """Requiring a name nothing has ever emitted is the DX-02 outage exactly.

    It is also how "the ruleset landed before the gate job did" presents: mit-learn's
    `ci-gate` sat at 0 until #3825 merged.
    """
    found = _evidence(produced=0, eligible=0)
    assert not found.safe
    assert found.verdict == "ABSENT"


def test_path_filtered_is_never_safe_however_clean_it_looks() -> None:
    """`Run zizmor` is 1/1 of the PRs that ran it, and requiring it blocks 39 in 40.

    Counting alone cannot see this, which is why the verdict reads the workflow's `on:`
    block instead. A perfect ratio over a filtered workflow is the trap.
    """
    found = _evidence(produced=1, eligible=1, filtered=True)
    assert not found.safe
    assert found.verdict == "FILTERED"


class TestPathFilterDetection:
    """`_workflow_at` decides `filtered` from the workflow file, not from history."""

    def _filtered(self, monkeypatch: pytest.MonkeyPatch, source: str) -> bool:
        monkeypatch.setattr(
            tool,
            "_gh",
            lambda *_: {"content": base64.b64encode(source.encode()).decode()},
        )
        result = tool._workflow_at("repo", ".github/workflows/w.yml", "sha")
        assert result is not None
        return result[1]

    def test_paths_filter_is_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The `Run zizmor` shape, and the reason the verdict reads the file."""
        assert self._filtered(
            monkeypatch,
            "on:\n  pull_request:\n    paths: ['.github/workflows/**']\njobs:\n"
            "  zizmor:\n    name: Run zizmor\n",
        )

    def test_paths_ignore_is_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other half of the filter syntax, equally disqualifying."""
        assert self._filtered(
            monkeypatch,
            "on:\n  pull_request:\n    paths-ignore: ['docs/**']\njobs:\n  test:\n",
        )

    def test_unfiltered_workflow_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`on: [push]` is the shorthand both mit-learn and mitxonline ci.yml use.

        It parses to a list rather than a mapping, and reading `.values()` off it would
        raise -- which, in a tool whose failures are silent, would be one more silent
        failure.
        """
        assert not self._filtered(monkeypatch, "on: [push]\njobs:\n  test:\n")

    def test_yaml_reads_bare_on_as_the_boolean_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """YAML 1.1, which pyyaml still implements, parses the key `on` as `True`.

        So a workflow whose filter is under an unquoted `on:` would look unfiltered --
        and an unfiltered path-filtered workflow is a requirable-looking `Run zizmor`.
        """
        source = "on:\n  push:\n    paths: ['a/**']\njobs:\n  test:\n"
        assert True in yaml.safe_load(source)
        assert self._filtered(monkeypatch, source)


class TestJobNameMatching:
    """A check-run is named for its job: `name:` if set, else the id, plus `(..)`."""

    def test_matches_the_job_id(self) -> None:
        """`ci-gate` sets no `name:`, so the id is what GitHub reports."""
        assert tool._defines({"test", "ci-gate"}, "ci-gate")

    def test_matches_a_matrix_shard(self) -> None:
        """One job named `python-tests` reports as `python-tests (1)`..`(4)`.

        The matrix values are not reconstructable from the workflow file without
        evaluating the matrix, so the match is by prefix.
        """
        assert tool._defines({"python-tests"}, "python-tests (3)")

    def test_does_not_match_an_unrelated_prefix(self) -> None:
        """`python-tests` must not vouch for a `python-tests-slow` nothing defines."""
        assert not tool._defines({"python-tests"}, "python-tests-slow")

    def test_a_job_the_commit_does_not_define_is_not_matched(self) -> None:
        """The case that makes `ci-gate` requirable: a branch cut before it existed."""
        assert not tool._defines({"test", "javascript-tests"}, "ci-gate")


class TestDriftExitStatus:
    """`drift` is only useful to CI if a reported failure also fails the process.

    Written after a revision of this file reported mit-learn's missing `ci-gate` in full
    and still exited 0, which in CI is indistinguishable from a clean fleet.
    """

    def _run(self, monkeypatch: pytest.MonkeyPatch, evidence: dict[str, Any]) -> int:
        monkeypatch.setattr(tool, "_declared", lambda: {"repo": ["ci-gate"]})
        monkeypatch.setattr(
            tool, "_merged", lambda *_: [{"number": 1, "headRefOid": "a"}]
        )
        monkeypatch.setattr(tool, "_read", lambda *_: (evidence, {}))
        try:
            tool.drift(prs=1)
        except SystemExit as exit_called:
            return int(exit_called.code or 0)
        return 0

    def test_a_context_nothing_produces_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering trap: the ruleset declared before the gate job merged."""
        assert self._run(monkeypatch, {}) == 1

    def test_a_context_that_stopped_being_produced_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A renamed job or a resized matrix, two repos away from the symptom."""
        assert (
            self._run(monkeypatch, {"ci-gate": _evidence(produced=3, eligible=4)}) == 1
        )

    def test_a_path_filtered_context_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clean on every PR that ran it, and still blocks the ones that did not."""
        assert (
            self._run(
                monkeypatch,
                {"ci-gate": _evidence(produced=4, eligible=4, filtered=True)},
            )
            == 1
        )

    def test_a_clean_fleet_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CI must stay green when nothing has drifted."""
        assert (
            self._run(monkeypatch, {"ci-gate": _evidence(produced=4, eligible=4)}) == 0
        )


class TestRateLimitIsNotSwallowed:
    """A 403 must not read as "this check was never produced".

    Written from a live run: a full sample of three repos exhausted the core quota, and
    the next command reported mit-learn's `openapi-diff` as ABSENT 0/0 minutes after
    `drift` had reported the same context clean. Under the old behaviour that verdict is
    indistinguishable from a context nothing produces, which is the one thing this tool
    exists to detect.
    """

    def _run(self, monkeypatch: pytest.MonkeyPatch, stderr: str, code: int) -> Any:
        class Result:
            returncode = code
            stdout = ""

        Result.stderr = stderr  # type: ignore[attr-defined]
        monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: Result())
        return tool._gh("api", "anything")

    def test_rate_limit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The core-quota message `gh` prints when the hourly budget is gone."""
        with pytest.raises(tool.RateLimitError):
            self._run(monkeypatch, "API rate limit exceeded for user ID 1", 1)

    def test_secondary_rate_limit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The abuse-detection variant, which reads differently and matters the same."""
        with pytest.raises(tool.RateLimitError):
            self._run(monkeypatch, "You have exceeded a secondary rate limit", 1)

    def test_a_missing_commit_is_still_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A force-pushed-away head is a sample we cannot read, not a reason to stop."""
        assert self._run(monkeypatch, "gh: Not Found (HTTP 404)", 1) is None


class TestPagination:
    """A check the tool did not read must not become a check that was never produced.

    Every endpoint `_checks_on` uses caps a page, and `/commits/{sha}/status` caps at 30
    rather than 100. Reading one page silently drops the rest, which lands as a lower
    `produced` against an unchanged `eligible` -- a SAFE name reported unsafe.
    """

    def _pages(self, monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> list[Any]:
        calls = iter(pages)
        monkeypatch.setattr(tool, "_gh", lambda *_a: next(calls, None))
        return tool._gh_all("repos/o/r/thing", "items")

    def test_follows_every_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        full = [{"n": i} for i in range(tool._PAGE)]
        rest = [{"n": "last"}]
        got = self._pages(
            monkeypatch,
            [{"items": full, "total_count": tool._PAGE + 1}, {"items": rest}],
        )
        assert len(got) == tool._PAGE + 1

    def test_stops_on_a_short_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A page below the cap is the last one, so nothing further is requested."""
        got = self._pages(monkeypatch, [{"items": [{"n": 1}]}, {"items": [{"n": 2}]}])
        assert got == [{"n": 1}]

    def test_stops_once_total_count_is_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`total_count` ends it even when the page came back exactly full."""
        full = [{"n": i} for i in range(tool._PAGE)]
        got = self._pages(
            monkeypatch, [{"items": full, "total_count": tool._PAGE}, {"items": full}]
        )
        assert len(got) == tool._PAGE

    def test_an_unreadable_endpoint_yields_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 on a garbage-collected head is still tolerated, not paged forever."""
        assert self._pages(monkeypatch, [None]) == []
