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
        ("SEC-06", {"_direct_collaborators": {"someone": "admin"}}),
        ("SEC-15", {"teams": {"arbisoft-contractors": "admin"}}),
        (
            "SEC-16",
            {
                "_branch_protection_push_restrictions": {
                    "users": ["odlbot"],
                    "teams": [],
                    "apps": [],
                },
                "_branch_protection_enforce_admins": False,
            },
        ),
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


def test_sec01_exempts_unmanaged_tier() -> None:
    """Forks and other `tier: unmanaged` repos have no org ruleset targeting them by
    design (§5.4, §9.2) -- reporting them as "unprotected" is noise, not a finding.
    """
    assert "SEC-01" not in fired(
        [repo(_has_branch_protection=False, _ruleset_count=0, tier="unmanaged")]
    )


def test_sec15_is_an_allowlist_not_a_denylist() -> None:
    """A team nobody has sanctioned must fire, which a denylist would miss."""
    assert "SEC-15" in fired([repo(teams={"a-brand-new-team": "admin"})])
    assert "SEC-15" not in fired([repo(teams={"devops": "admin"})])
    # Non-admin from an unsanctioned team is fine -- the rule is about the level.
    assert "SEC-15" not in fired([repo(teams={"arbisoft-contractors": "push"})])


def _restricted(**overrides: Any) -> dict[str, Any]:
    """Build a repo whose default branch only `odlbot` may push to."""
    return repo(
        **{
            "_branch_protection_push_restrictions": {
                "users": ["odlbot"],
                "teams": [],
                "apps": [],
            },
            "_branch_protection_enforce_admins": False,
            **overrides,
        }
    )


def test_sec16_needs_a_restriction_to_fire() -> None:
    """No restriction is the normal state -- `push` alone must stay silent."""
    assert "SEC-16" not in fired([repo(teams={"odl-engineering": "push"})])


def test_sec16_ignores_grants_that_could_never_merge() -> None:
    """An allow-list takes nothing from `pull` or `triage`, so neither is a finding."""
    assert "SEC-16" not in fired([_restricted(teams={"odl-engineering": "pull"})])
    assert "SEC-16" not in fired([_restricted(teams={"odl-engineering": "triage"})])


def test_sec16_follows_enforce_admins_for_admin_teams() -> None:
    """The exact mechanism behind the open-edx-plugins outage.

    With `enforce_admins` off an admin team bypasses the allow-list and is genuinely
    not blocked; the same team at `push` is. Reporting the admin case would be a false
    positive, and NOT reporting the push case is the miss that let this run for days.
    """
    admins = {"odl-engineering": "admin"}
    assert "SEC-16" not in fired([_restricted(teams=admins)])
    assert "SEC-16" in fired(
        [_restricted(teams=admins, _branch_protection_enforce_admins=True)]
    )
    assert "SEC-16" in fired([_restricted(teams={"odl-engineering": "push"})])


def test_sec16_clears_when_the_team_is_on_the_allow_list() -> None:
    listed = _restricted(teams={"odl-engineering": "push"})
    listed["_branch_protection_push_restrictions"]["teams"] = ["odl-engineering"]
    assert "SEC-16" not in fired([listed])


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
    findings = audit.evaluate(
        [repo(secret_scanning="disabled")]  # pragma: allowlist secret
    )
    assert findings
    for finding in findings:
        assert finding.remediation
        assert finding.expected


def test_con12_fires_on_an_active_repo_with_no_team_grants() -> None:
    """The 80-repo finding rests entirely on this, so it gets a case of its own.

    The org's `default_repository_permission` is `none`, which is what makes an empty
    `teams` block mean "nobody but an org owner can reach this" rather than nothing at
    all. If that org setting ever changes, this rule stops measuring what it claims,
    and this test is where to start looking.
    """
    assert "CON-12" in fired([repo(teams={})])
    assert "CON-12" in fired([repo(teams=None)])
    assert "CON-12" not in fired([repo(teams={"odl-engineering": "push"})])


def test_con12_is_scoped_to_active_repos() -> None:
    """Archived repos emit no TeamRepository at all, so the rule cannot act on them.

    Without this case a scope regression would silently inflate the finding with the
    archived repos that also grant nothing -- a number that reads as new work but is
    unreachable by this project.
    """
    assert "CON-12" not in fired([repo(archived=True, teams={})])


# --- classify_direct_grants -------------------------------------------------------
#
# These decide which grants get advertised as safe to delete, so each of the five
# kinds is pinned. A misclassification here is not a wrong report, it is a deletion
# that revokes someone's access.

ROSTERS = {"odl-engineering": {"alice"}, "devops": {"bob"}, "copilot": {"carol"}}
PARENTS: dict[str, str | None] = {
    "copilot": "odl-engineering",
    "odl-engineering": None,
    "devops": None,
}
MEMBERS = {"alice", "bob", "carol", "owner"}
OWNERS = {"owner"}


def classify(**repo_overrides: Any) -> str:
    """Classify the single direct grant on a one-repo fleet."""
    rows = audit.classify_direct_grants(
        [repo(**repo_overrides)], ROSTERS, MEMBERS, PARENTS, OWNERS
    )
    assert len(rows) == 1
    return str(rows[0]["kind"])


def test_grant_is_redundant_when_team_access_already_meets_it() -> None:
    assert (
        classify(
            teams={"odl-engineering": "maintain"},
            _direct_collaborators={"alice": "write"},
        )
        == "redundant"
    )


def test_grant_is_level_only_when_team_access_is_lower() -> None:
    """Removal keeps repo reach and drops the elevated rights -- the SEC-15 target."""
    assert (
        classify(
            teams={"odl-engineering": "push"},
            _direct_collaborators={"alice": "admin"},
        )
        == "level-only"
    )


def test_grant_is_no_access_when_no_team_covers_the_person() -> None:
    assert (
        classify(teams={"devops": "admin"}, _direct_collaborators={"alice": "admin"})
        == "no-access"
    )


def test_grant_is_outside_when_the_person_is_not_an_org_member() -> None:
    assert classify(teams={}, _direct_collaborators={"stranger": "write"}) == "outside"


def test_org_owners_are_classified_by_ownership_not_by_teams() -> None:
    """Ownership is a third access path, and the one teams cannot take away.

    Ranking by teams alone put 10 owner-held grants in `no-access` -- the bucket that
    means "removing this revokes access" -- overstating the gating set by nearly half.
    An owner keeps implicit admin whatever the rosters do, so the answer must not
    depend on them: both cases below are the same verdict.
    """
    assert classify(teams={}, _direct_collaborators={"owner": "admin"}) == (
        "owner-implicit"
    )
    assert (
        classify(
            teams={"odl-engineering": "push"},
            _direct_collaborators={"owner": "admin"},
        )
        == "owner-implicit"
    )


def test_nested_team_members_inherit_the_parents_grant() -> None:
    """`copilot` is a child of `odl-engineering`, so a grant to the parent covers it.

    Matching team slugs exactly would classify every child-team member as having no
    team access, manufacturing `no-access` findings for grants that are redundant.
    """
    assert (
        classify(
            teams={"odl-engineering": "admin"},
            _direct_collaborators={"carol": "admin"},
        )
        == "redundant"
    )


def test_write_and_push_are_the_same_rung() -> None:
    """GitHub says `write` for collaborators and `push` for teams.

    Comparing the raw strings would score every write grant as unmatched against a
    push team, and report it as an elevation that needs removing.
    """
    assert (
        classify(
            teams={"odl-engineering": "push"},
            _direct_collaborators={"alice": "write"},
        )
        == "redundant"
    )


def test_removable_kinds_exclude_the_two_that_cost_something() -> None:
    """The headline "N removable today" is summed over this set."""
    assert "no-access" not in audit.REMOVABLE_KINDS
    assert "outside" not in audit.REMOVABLE_KINDS
