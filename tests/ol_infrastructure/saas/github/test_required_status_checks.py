"""Tests for the `required_status_checks` fleet-data guard.

This validator is unlike the others in archetypes.py: it is not standing between bad
data and a crash, it is standing between bad data and a SILENT OUTAGE. rulesets.py will
build a ruleset from any list of strings and GitHub will accept it, and because the
Team plan has no `evaluate` enforcement mode there is no state between "not
enforced" and "enforced on the org's busiest repos". A context nothing produces
leaves every PR on that repo permanently pending.

So the cases worth writing are the ones where the data is well-formed and still wrong.
"""

from typing import Any

import pytest

from ol_infrastructure.saas.github.repositories import archetypes


def _check(*repos: dict[str, Any]) -> None:
    archetypes._check_required_status_checks(list(repos))


def test_no_declaration_is_fine() -> None:
    """Most of the fleet declares nothing, and that must stay silent."""
    _check({"name": "quiet"})
    _check({"name": "quiet", "required_status_checks": []})


def test_accepts_plain_context_names() -> None:
    _check({"name": "ol-infrastructure", "required_status_checks": ["test"]})


def test_rejects_declaration_on_an_archived_repo() -> None:
    """GitHub refuses ruleset writes on an archived repo, so it is unsatisfiable."""
    with pytest.raises(ValueError, match="archived"):
        _check({"name": "old", "archived": True, "required_status_checks": ["test"]})


@pytest.mark.parametrize("bad", [[""], ["   "], [None], ["ok", ""], "test"])
def test_rejects_empty_or_non_string_contexts(bad: Any) -> None:
    """An empty context blocks the branch while naming nothing in the UI.

    A bare string rather than a list is in here because YAML makes it easy: writing
    `required_status_checks: test` silently yields the string, which would iterate into
    four single-character contexts.
    """
    with pytest.raises(ValueError, match="list of non-empty strings"):
        _check({"name": "repo", "required_status_checks": bad})


def test_rejects_matrix_shard_names_without_the_opt_in() -> None:
    """Mitxonline's `python-tests (1)`..`(4)` are the live example.

    In February 2026 that repo produced one check named `python-tests`; a commit on
    2026-07-23 sharded it four ways and the old name has never appeared since. Requiring
    a shard name is legal and works today, which is exactly why it needs to be
    deliberate: the edit that breaks it happens in a different repository.
    """
    with pytest.raises(ValueError, match="matrix shard names"):
        _check({"name": "mitxonline", "required_status_checks": ["python-tests (1)"]})


def test_allows_matrix_shard_names_when_opted_in() -> None:
    _check(
        {
            "name": "mitxonline",
            "required_status_checks": ["python-tests (1)", "python-tests (2)"],
            archetypes.MATRIX_OPT_IN: True,
        }
    )


def test_error_names_every_offender_at_once() -> None:
    """Reporting one repo per run makes fixing a fleet a game of whack-a-mole."""
    with pytest.raises(ValueError, match="would block merges") as caught:
        _check(
            {"name": "alpha", "archived": True, "required_status_checks": ["test"]},
            {"name": "beta", "required_status_checks": ["shard (1)"]},
        )
    assert "alpha" in str(caught.value)
    assert "beta" in str(caught.value)


def test_error_points_at_the_verification_tool() -> None:
    """The fix is always "go check what the repo actually produces"."""
    with pytest.raises(ValueError, match="github-required-checks"):
        _check({"name": "repo", "required_status_checks": ["thing (1)"]})
