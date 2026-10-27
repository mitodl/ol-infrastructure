"""Tests for how `bin/github-org-inventory` reads managed rulesets back into the data.

`crawl` rebuilds every repo YAML from live state, so a declared value the crawl does
not read back is deleted by the next crawl, and Pulumi then wants to remove the
ruleset it described. These tests cover the strict flag and Copilot review, using
the live shapes of the hand-made rulesets on lehrer and ol-analytics-api.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "bin" / "github-org-inventory"


def _load() -> Any:
    loader = SourceFileLoader("github_org_inventory_readback", str(SCRIPT))
    spec = importlib.util.spec_from_file_location(loader.name, SCRIPT, loader=loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load {SCRIPT}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


def _serve(monkeypatch: pytest.MonkeyPatch, details: dict[int, dict[str, Any]]) -> None:
    """Answer `_maybe` ruleset-detail lookups from `details`, keyed by ruleset id."""

    def fake_maybe(_client: Any, path: str) -> Any:
        return details.get(int(path.rsplit("/", 1)[-1]))

    monkeypatch.setattr(tool, "_maybe", fake_maybe)


def _listed(ruleset_id: int, name: str, source_type: str = "Repository") -> Any:
    return {"id": ruleset_id, "name": name, "source_type": source_type}


def _checks_rule(contexts: list[str], *, strict: bool) -> dict[str, Any]:
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": strict,
            "required_status_checks": [{"context": c} for c in contexts],
        },
    }


def test_strict_comes_from_our_ruleset_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """ol-analytics-api's hand-made "main protection" is strict. That must not be
    copied into the data, the same way its contexts are kept apart as foreign.
    """
    _serve(
        monkeypatch,
        {
            1: {
                "name": "main protection",
                "rules": [_checks_rule(["test"], strict=True)],
            },
            2: {
                "name": tool.REQUIRED_CHECKS_RULESET_NAME,
                "rules": [_checks_rule(["prek", "test"], strict=False)],
            },
        },
    )
    rulesets = [
        _listed(1, "main protection"),
        _listed(2, tool.REQUIRED_CHECKS_RULESET_NAME),
    ]
    assert tool._required_checks(None, "repo", None, rulesets) == (
        ["prek", "test"],
        ["test"],
        False,
    )


def test_strict_reads_back_when_ours_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        {
            2: {
                "name": tool.REQUIRED_CHECKS_RULESET_NAME,
                "rules": [_checks_rule(["test"], strict=True)],
            }
        },
    )
    rulesets = [_listed(2, tool.REQUIRED_CHECKS_RULESET_NAME)]
    assert tool._required_checks(None, "repo", None, rulesets)[2] is True


def _copilot_detail(name: str, *, drafts: bool) -> dict[str, Any]:
    return {
        "name": name,
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "copilot_code_review",
                "parameters": {
                    "review_draft_pull_requests": drafts,
                    "review_on_push": False,
                },
            },
        ],
    }


def test_copilot_review_reads_back_our_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch, {3: _copilot_detail(tool.COPILOT_REVIEW_RULESET_NAME, drafts=True)}
    )
    rulesets = [_listed(3, tool.COPILOT_REVIEW_RULESET_NAME)]
    assert tool._copilot_review(None, "repo", rulesets) == {
        "review_draft_pull_requests": True,
        "review_on_push": False,
    }


def test_copilot_review_ignores_a_hand_made_ruleset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-made "Copilot review for default branch" rulesets must not be copied
    into the data, or a crawl would declare them without anyone deciding to.
    """
    hand_made = "Copilot review for default branch"
    _serve(monkeypatch, {4: _copilot_detail(hand_made, drafts=False)})
    assert tool._copilot_review(None, "repo", [_listed(4, hand_made)]) is None


def test_copilot_review_ignores_inherited_org_rulesets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch, {5: _copilot_detail(tool.COPILOT_REVIEW_RULESET_NAME, drafts=True)}
    )
    rulesets = [_listed(5, tool.COPILOT_REVIEW_RULESET_NAME, "Organization")]
    assert tool._copilot_review(None, "repo", rulesets) is None
