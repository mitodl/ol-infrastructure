"""Tests for the estate audit rules.

The rules are pure functions over a merged archetype+repo dict, so every case here is
a literal -- no crawl, no fixtures on disk, no credentials. That is the property the
phase-4 design was chosen for, and these tests are what make it worth having.

The cases that earn their keep are the SCOPE ones. A rule measured against the wrong
population is not wrong by a little; it reports a real number about a different set of
repos than it claims. That mistake has been made twice on this project, both times
caught by cross-checking a percentage against raw data rather than by reading the code.
"""

from typing import Any

import pytest

from ol_infrastructure.saas.github import audit


def repo(**overrides: Any) -> dict[str, Any]:
    """Return a repo that fires no rules, so each test breaks exactly one thing."""
    base: dict[str, Any] = {
        "name": "example",
        "archetype": "library",
        "archived": False,
        "description": "a repo",
        "topics": ["python"],
        "default_branch": "main",
        "allow_auto_merge": True,
        "delete_branch_on_merge": True,
        "dependabot_security_updates": True,
        "secret_scanning": "enabled",  # pragma: allowlist secret
        "secret_scanning_push_protection": "enabled",  # pragma: allowlist secret
        "teams": {"odl-engineering-owners": "admin", "odl-engineering": "push"},
        "_has_branch_protection": True,
        "_ruleset_count": 1,
        "_pushed_at": "2026-08-01T00:00:00Z",
        "_visibility": "public",
    }
    return {**base, **overrides}


def fired(fleet: list[dict[str, Any]]) -> set[str]:
    return {f.rule_id for f in audit.evaluate(fleet)}


def test_clean_repo_fires_nothing() -> None:
    assert fired([repo()]) == set()


@pytest.mark.parametrize(
    ("rule_id", "overrides"),
    [
        ("SEC-01", {"_has_branch_protection": False, "_ruleset_count": 0}),
        ("SEC-04", {"secret_scanning": "disabled"}),  # pragma: allowlist secret
        ("SEC-05", {"dependabot_security_updates": False}),
        ("SEC-06", {"_direct_collaborators": {"someone": "admin"}}),
        ("SEC-15", {"teams": {"arbisoft-contractors": "admin"}}),
        ("CON-02", {"delete_branch_on_merge": False}),
        ("CON-03", {"default_branch": "master"}),
        ("CON-05", {"topics": []}),
        ("CON-10", {"description": None}),
        ("DX-01", {"allow_auto_merge": False}),
        ("DX-07", {"_pushed_at": "2020-01-01T00:00:00Z"}),
        ("DX-08", {"_excluded_environments": 500}),
    ],
)
def test_each_rule_fires_on_its_own_trigger(rule_id: str, overrides: Any) -> None:
    assert rule_id in fired([repo(**overrides)])


def test_sec01_needs_both_missing() -> None:
    """A ruleset alone is protection, and so is branch protection alone."""
    assert "SEC-01" not in fired([repo(_has_branch_protection=False, _ruleset_count=1)])
    assert "SEC-01" not in fired([repo(_has_branch_protection=True, _ruleset_count=0)])


def test_sec15_is_an_allowlist_not_a_denylist() -> None:
    """A team nobody has sanctioned must fire, which a denylist would miss."""
    assert "SEC-15" in fired([repo(teams={"a-brand-new-team": "admin"})])
    assert "SEC-15" not in fired([repo(teams={"devops": "admin"})])
    # Non-admin from an unsanctioned team is fine -- the rule is about the level.
    assert "SEC-15" not in fired([repo(teams={"arbisoft-contractors": "push"})])


def test_con03_exempts_forks_and_archived() -> None:
    """102 active repos default to `master` and nearly all are forks."""
    assert "CON-03" not in fired([repo(archetype="fork", default_branch="master")])
    assert "CON-03" in fired([repo(archetype="library", default_branch="master")])


def test_archived_repos_are_out_of_scope_for_active_rules() -> None:
    """An archived repo must not be reported for things nobody can change on it."""
    archived = repo(archived=True, topics=[], description=None, allow_auto_merge=False)
    assert fired([archived]).isdisjoint({"CON-05", "CON-10", "DX-01"})


def test_con07_only_looks_at_archived_repos() -> None:
    """Its whole subject is archived repos; on an active one it is meaningless."""
    grants = {"odl-engineering": "push"}
    assert "CON-07" in fired([repo(archived=True, teams=grants)])
    assert "CON-07" not in fired([repo(archived=False, teams=grants)])


def test_fleet_scoped_rules_cover_archived_repos() -> None:
    """SEC-06 and SEC-15 have no exemptions -- an admin grant on an archived repo is
    inert only until someone unarchives it.
    """
    archived = repo(archived=True, _direct_collaborators={"someone": "admin"})
    assert "SEC-06" in fired([archived])


def test_population_matches_each_rules_scope() -> None:
    """The denominator a percentage is reported against must be the set measured."""
    fleet = [repo(name="a"), repo(name="b", archived=True), repo(name="c")]
    assert audit.population(fleet, "active") == 2
    assert audit.population(fleet, "archived") == 1
    assert audit.population(fleet, "fleet") == 3


def test_every_rule_declares_a_known_axis_and_scope() -> None:
    """Guards the reporter, which indexes findings by both."""
    for rule in audit.RULES:
        assert rule.axis in ("security", "consistency", "developer-experience")
        assert rule.scope in ("active", "archived", "fleet")
        assert rule.severity in ("high", "medium", "low")


def test_rule_ids_are_unique() -> None:
    ids = [rule.rule_id for rule in audit.RULES]
    assert len(ids) == len(set(ids))


def test_findings_carry_remediation() -> None:
    """A finding without a next action is a complaint, not a backlog item."""
    findings = audit.evaluate([repo(dependabot_security_updates=False)])
    assert findings
    for finding in findings:
        assert finding.remediation
        assert finding.expected
