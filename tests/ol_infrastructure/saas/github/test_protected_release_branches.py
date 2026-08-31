"""Tests for the `protected_release_branches` fleet-data guard.

Same shape as `test_required_status_checks.py`: `rulesets.py` will build a ruleset
from any list of strings and GitHub will accept it, so the cases worth writing are
the ones where the data is well-formed and still wrong.
"""

from typing import Any

import pytest

from ol_infrastructure.saas.github.repositories import archetypes


def _check(*repos: dict[str, Any]) -> None:
    archetypes._check_protected_release_branches(list(repos))


def test_no_declaration_is_fine() -> None:
    """Most of the fleet declares nothing, and that must stay silent."""
    _check({"name": "quiet"})
    _check({"name": "quiet", "protected_release_branches": []})


def test_accepts_plain_branch_names() -> None:
    """A well-formed declaration passes through untouched."""
    _check(
        {
            "name": "mit-learn",
            "protected_release_branches": ["release", "release-candidate"],
        }
    )


def test_rejects_declaration_on_an_archived_repo() -> None:
    """GitHub refuses ruleset writes on an archived repo, so it is unsatisfiable."""
    with pytest.raises(ValueError, match="archived"):
        _check(
            {
                "name": "old",
                "archived": True,
                "protected_release_branches": ["release"],
            }
        )


@pytest.mark.parametrize("bad", [[""], ["   "], [None], ["release", ""], "release"])
def test_rejects_empty_or_non_string_branches(bad: Any) -> None:
    """A bare string rather than a list is in here because YAML makes it easy:
    writing `protected_release_branches: release` silently yields the string, which
    would iterate into single-character branch names.
    """
    with pytest.raises(ValueError, match="list of non-empty strings"):
        _check({"name": "repo", "protected_release_branches": bad})


def test_error_names_every_offender_at_once() -> None:
    """Reporting one repo per run makes fixing a fleet a game of whack-a-mole."""
    with pytest.raises(ValueError, match="protected_release_branches") as caught:
        _check(
            {
                "name": "alpha",
                "archived": True,
                "protected_release_branches": ["release"],
            },
            {"name": "beta", "protected_release_branches": [""]},
        )
    assert "alpha" in str(caught.value)
    assert "beta" in str(caught.value)
