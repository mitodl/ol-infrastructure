"""Tests for the `required_status_checks_strict` and `copilot_code_review` guard.

rulesets.py reads both with `bool()`, so bad data there gives a wrong answer
instead of an error. These cases are the ones where that would happen.
"""

from typing import Any

import pytest

from ol_infrastructure.saas.github.repositories import archetypes


def _check(*repos: dict[str, Any]) -> None:
    archetypes._check_ruleset_options(list(repos))


def test_no_declaration_is_fine() -> None:
    """Most of the fleet declares neither, and that must stay silent."""
    _check({"name": "quiet"})


def test_accepts_well_formed_declarations() -> None:
    _check(
        {
            "name": "ol-analytics-api",
            "required_status_checks": ["test"],
            "required_status_checks_strict": True,
        },
        {
            "name": "open-edx-plugins",
            "copilot_code_review": {
                "review_draft_pull_requests": True,
                "review_on_push": False,
            },
        },
    )


def test_rejects_a_string_strict_flag() -> None:
    """`"false"` is a non-empty string, so `bool()` would turn it into true."""
    with pytest.raises(ValueError, match="must be a bool"):
        _check(
            {
                "name": "repo",
                "required_status_checks": ["test"],
                "required_status_checks_strict": "false",
            }
        )


def test_rejects_strict_without_required_checks() -> None:
    """With no checks there is no ruleset for the flag to apply to."""
    with pytest.raises(ValueError, match="needs required_status_checks"):
        _check({"name": "repo", "required_status_checks_strict": True})


def test_rejects_copilot_review_on_an_archived_repo() -> None:
    """GitHub refuses ruleset writes on an archived repo, so it is unsatisfiable."""
    with pytest.raises(ValueError, match="archived"):
        _check(
            {
                "name": "old",
                "archived": True,
                "copilot_code_review": {"review_on_push": False},
            }
        )


@pytest.mark.parametrize("bad", [{}, True, "yes", ["review_on_push"]])
def test_rejects_a_copilot_review_that_is_not_a_mapping(bad: Any) -> None:
    """`copilot_code_review: true` reads naturally in YAML and would build nothing."""
    with pytest.raises(ValueError, match="non-empty mapping"):
        _check({"name": "repo", "copilot_code_review": bad})


def test_rejects_a_misspelled_copilot_parameter() -> None:
    """A typo would be dropped and the real parameter would default to false."""
    with pytest.raises(ValueError, match="unknown keys"):
        _check({"name": "repo", "copilot_code_review": {"review_on_pusj": True}})


def test_rejects_a_non_bool_copilot_parameter() -> None:
    with pytest.raises(ValueError, match="must be bools"):
        _check({"name": "repo", "copilot_code_review": {"review_on_push": "no"}})


def test_error_names_every_offender_at_once() -> None:
    """Reporting one repo per run makes fixing a fleet a game of whack-a-mole."""
    with pytest.raises(ValueError, match="ruleset options") as caught:
        _check(
            {"name": "alpha", "required_status_checks_strict": True},
            {"name": "beta", "copilot_code_review": {}},
        )
    assert "alpha" in str(caught.value)
    assert "beta" in str(caught.value)
