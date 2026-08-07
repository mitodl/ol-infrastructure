"""Estate audit rules: pure functions over the resolved fleet model.

Phase 4 of docs/plans/github-org-pulumi-import.md. Every rule takes the merged
archetype+repo dict and returns a Finding or None, which is what makes them
trivially testable with fixtures and cheap to add -- the rule set is meant to grow
every time someone notices something.

WHY THIS READS COMMITTED DATA RATHER THAN THE API. `data/repos/*.yaml` carries both
the configuration Pulumi manages and, under `_`-prefixed keys, the observed state the
rules need. So the audit is a dictionary comparison, not a crawl: it runs in
milliseconds, needs no credentials, works in CI, and -- because the data is in git --
`git log` over data/repos/ is a history of the estate. `drift` is the one mode that
does hit the API, precisely because its job is to compare the two.

SCOPE IS PART OF THE RULE. A rule that reports "94 of 138 archived" as a share of
active repos is not wrong by a little, it is measuring a different population than it
claims. Each rule declares its own `scope`, and the reporter uses that as the
denominator. Getting this wrong has already happened twice on this project.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ol_infrastructure.saas.github.tiers import TIER_PROPERTY_NAME

Axis = Literal["security", "consistency", "developer-experience"]
Scope = Literal["active", "archived", "fleet"]
Severity = Literal["high", "medium", "low"]

#: A repo is an archive candidate (DX-07) after this long with no push.
STALE_DAYS = 365
#: Environments beyond this on one repo means CI is leaking review apps (DX-08).
ENVIRONMENT_SPRAWL_THRESHOLD = 10
#: Teams sanctioned to hold `admin` (SEC-15). An ALLOWLIST on purpose: under a
#: denylist a newly created team acquires admin silently and the rule stays quiet.
ADMIN_TEAMS = frozenset({"odl-engineering-owners", "devops"})
#: Archetypes exempt from the default-branch rule. Forks follow upstream's naming.
DEFAULT_BRANCH_EXEMPT = frozenset({"fork", "archived"})
FEATURE_ENABLED = "enabled"


@dataclass(frozen=True)
class Finding:
    """One rule firing on one repo."""

    rule_id: str
    axis: Axis
    severity: Severity
    repo: str
    current: str
    expected: str
    remediation: str


@dataclass(frozen=True)
class Rule:
    """A rule and the population it is measured against."""

    rule_id: str
    axis: Axis
    severity: Severity
    scope: Scope
    summary: str
    check: Callable[[dict[str, Any]], tuple[str, str, str] | None]
    """Returns (current, expected, remediation) when the rule fires, else None."""


def _in_scope(repo: dict[str, Any], scope: Scope) -> bool:
    archived = bool(repo.get("archived"))
    if scope == "fleet":
        return True
    return archived if scope == "archived" else not archived


def _is_stale(repo: dict[str, Any]) -> bool:
    pushed = repo.get("_pushed_at")
    if not pushed:
        return False
    age = datetime.now(UTC) - datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    return age.days > STALE_DAYS


def _live_tier(repo: dict[str, Any]) -> str | None:
    """Return the `tier` GitHub actually reports for this repo, per the last crawl.

    `None` means the crawl recorded no value -- either the repo YAML predates
    `_custom_properties` or the property genuinely came back empty. Both are findings,
    and CON-11 reports them as such rather than passing. `tier` is `required` with a
    default, so there is no live state in which the correct answer is "no value".
    """
    return (repo.get("_custom_properties") or {}).get(TIER_PROPERTY_NAME)


def _unsanctioned_admin(repo: dict[str, Any]) -> list[str]:
    return sorted(
        team
        for team, perm in (repo.get("teams") or {}).items()
        if perm == "admin" and team not in ADMIN_TEAMS
    )


RULES: tuple[Rule, ...] = (
    Rule(
        "SEC-01",
        "security",
        "high",
        "active",
        "default branch has no ruleset and no branch protection",
        lambda r: (
            (
                "unprotected",
                "covered by an org ruleset",
                "set `tier` so a ruleset matches",
            )
            if not r.get("_has_branch_protection") and not r.get("_ruleset_count")
            else None
        ),
    ),
    Rule(
        "SEC-04",
        "security",
        "high",
        "active",
        "secret scanning or push protection disabled",
        lambda r: (
            (
                f"scanning={r.get('secret_scanning')} "
                f"push_protection={r.get('secret_scanning_push_protection')}",
                "both enabled",
                "enable in repo settings, or org-wide for new repos",
            )
            if r.get("secret_scanning") != FEATURE_ENABLED
            or r.get("secret_scanning_push_protection") != FEATURE_ENABLED
            else None
        ),
    ),
    Rule(
        "SEC-05",
        "security",
        "high",
        "active",
        "Dependabot security updates disabled",
        lambda r: (
            ("disabled", "enabled", "set dependabot_security_updates in the archetype")
            if not r.get("dependabot_security_updates")
            else None
        ),
    ),
    Rule(
        "SEC-06",
        "security",
        "high",
        "fleet",
        "individual holds a direct permission on a repo",
        lambda r: (
            (
                ", ".join(
                    f"{u}:{p}"
                    for u, p in sorted((r.get("_direct_collaborators") or {}).items())
                ),
                "no direct grants; access via team membership",
                "move the person into a team that already has the right grant",
            )
            if r.get("_direct_collaborators")
            else None
        ),
    ),
    Rule(
        "SEC-15",
        "security",
        "high",
        "fleet",
        "admin held by a team outside the sanctioned set",
        lambda r: (
            (
                ", ".join(_unsanctioned_admin(r)),
                f"admin only for {', '.join(sorted(ADMIN_TEAMS))}",
                "downgrade to push or maintain",
            )
            if _unsanctioned_admin(r)
            else None
        ),
    ),
    Rule(
        "CON-11",
        "consistency",
        "high",
        "fleet",
        "live `tier` does not match the declared tier",
        # The one field where declared-vs-live divergence changes which org rulesets
        # apply, so it is graded `high` despite being a consistency rule -- a repo at
        # the wrong tier is protected by a different baseline than the code claims.
        #
        # WRITTEN AS A THREE-WAY COMPARISON ON PURPOSE. The tempting form,
        # `if declared and declared != live`, passes silently whenever either side is
        # missing, and a missing live value is precisely how the 140-repo archived-repo
        # divergence stayed invisible: those repos were not untiered, they had fallen
        # into the property's `standard` default. Absence on either side is reported.
        lambda r: (
            (
                f"live {_live_tier(r) or 'unrecorded'}",
                f"declared {r.get('tier') or 'nothing'}",
                "re-run `github-org-inventory crawl --refresh`; if the live value is "
                "real, `pulumi up` the repositories stack to rewrite it",
            )
            if _live_tier(r) != r.get("tier")
            else None
        ),
    ),
    Rule(
        "CON-02",
        "consistency",
        "low",
        "active",
        "delete_branch_on_merge disabled",
        lambda r: (
            ("disabled", "enabled", "inherit from the base archetype")
            if not r.get("delete_branch_on_merge")
            else None
        ),
    ),
    Rule(
        "CON-03",
        "consistency",
        "low",
        "active",
        "default branch is not `main` (forks and archived exempt)",
        lambda r: (
            (r.get("default_branch", "?"), "main", "rename the default branch")
            if r.get("default_branch") not in (None, "main")
            and r.get("archetype") not in DEFAULT_BRANCH_EXEMPT
            else None
        ),
    ),
    Rule(
        "CON-05",
        "consistency",
        "low",
        "active",
        "no topics",
        lambda r: (
            ("none", "at least one", "add topics to the repo's YAML")
            if not r.get("topics")
            else None
        ),
    ),
    Rule(
        "CON-07",
        "consistency",
        "medium",
        "archived",
        "archived repo still grants a team write or better",
        lambda r: (
            (
                ", ".join(
                    f"{t}:{p}"
                    for t, p in sorted((r.get("teams") or {}).items())
                    if p in ("push", "admin", "maintain")
                ),
                "read-only access",
                "downgrade the grants, or accept and document",
            )
            if any(
                p in ("push", "admin", "maintain")
                for p in (r.get("teams") or {}).values()
            )
            else None
        ),
    ),
    Rule(
        "CON-10",
        "consistency",
        "low",
        "active",
        "no description",
        lambda r: (
            ("empty", "a one-line description", "add `description` to the repo's YAML")
            if not r.get("description")
            else None
        ),
    ),
    Rule(
        "DX-01",
        "developer-experience",
        "low",
        "active",
        "allow_auto_merge disabled",
        lambda r: (
            ("disabled", "enabled", "inherit from the base archetype")
            if not r.get("allow_auto_merge")
            else None
        ),
    ),
    Rule(
        "DX-07",
        "developer-experience",
        "low",
        "active",
        "no push in 12+ months -- archive candidate",
        lambda r: (
            (
                f"last push {r.get('_pushed_at', '?')[:10]}",
                "active development",
                "archive it",
            )
            if _is_stale(r)
            else None
        ),
    ),
    Rule(
        "DX-08",
        "developer-experience",
        "medium",
        "active",
        "environment sprawl -- CI is leaking review apps",
        lambda r: (
            (
                f"{r.get('_excluded_environments')} environments",
                f"at most {ENVIRONMENT_SPRAWL_THRESHOLD}",
                "have CI delete review-app environments when the PR closes",
            )
            if (r.get("_excluded_environments") or 0) > ENVIRONMENT_SPRAWL_THRESHOLD
            else None
        ),
    ),
)


def evaluate(fleet: Iterable[dict[str, Any]]) -> list[Finding]:
    """Run every rule over every in-scope repo."""
    findings: list[Finding] = []
    for repo in fleet:
        for rule in RULES:
            if not _in_scope(repo, rule.scope):
                continue
            result = rule.check(repo)
            if result is None:
                continue
            current, expected, remediation = result
            findings.append(
                Finding(
                    rule_id=rule.rule_id,
                    axis=rule.axis,
                    severity=rule.severity,
                    repo=repo["name"],
                    current=current,
                    expected=expected,
                    remediation=remediation,
                )
            )
    return findings


def population(fleet: Iterable[dict[str, Any]], scope: Scope) -> int:
    """How many repos a scope covers -- the honest denominator for a percentage."""
    return sum(1 for repo in fleet if _in_scope(repo, scope))
