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

from collections import Counter
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
#: Teams that exist for something other than repository access, and are therefore
#: expected to hold no grant. `copilot` assigns Copilot seats; the two `vault-*`
#: teams gate Vault secrets and are `privacy: secret` (see organization/teams.py).
#: Without this list, "holds no repo grant" would read as a finding on all three.
NON_ACCESS_TEAMS = frozenset(
    {"copilot", "vault-developer-access", "vault-devops-access"}
)
#: GitHub reports collaborator roles as `write`/`read` and team grants as
#: `push`/`pull`. Same rung, different vocabulary -- comparing the raw strings
#: would score every `write` collaborator as unmatched against a `push` team.
PERMISSION_RANK: dict[str, int] = {
    "read": 0,
    "pull": 0,
    "triage": 1,
    "write": 2,
    "push": 2,
    "maintain": 3,
    "admin": 4,
}
RANK_NAME: dict[int, str] = {
    0: "read",
    1: "triage",
    2: "push",
    3: "maintain",
    4: "admin",
}
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


#: Team grants that are supposed to carry the ability to merge a pull request.
#: `triage` and `pull` are excluded because neither can merge anyway, so being left out
#: of a push allow-list takes nothing away from them.
MERGE_CAPABLE_PERMISSIONS = frozenset({"push", "maintain", "admin"})


def _blocked_by_push_restriction(repo: dict[str, Any]) -> list[str]:
    """Teams holding a merge-capable grant that the push allow-list leaves out.

    Classic branch protection's "Restrict who can push" is an allow-list, and GitHub
    counts merging a pull request as a push -- so a team missing from it cannot merge no
    matter what `teams` says. Neither field shows this alone: `teams` reads `push` and
    looks fine, `_has_branch_protection` reads `true` and also looks fine. Only the pair
    reveals it, which is exactly why it went unseen.

    THE INCIDENT THIS RULE IS FOR. `open-edx-plugins` restricted pushes to `main` to the
    single user `odlbot`. `arbisoft-contractors` and `odl-engineering` both held
    `admin`, and because `enforce_admins` was false they bypassed the allow-list without
    anyone noticing it existed. PR #5324 (SEC-15, 2026-08-12) downgraded both to `push`
    -- the correct call on its own terms -- and in doing so removed the bypass, leaving
    two teams unable to merge anything. No rule fired, no preview showed a diff, and it
    surfaced five days later as a contractor reporting a stuck pull request.

    `enforce_admins` is therefore load-bearing rather than trivia: with it off, an
    `admin` team is genuinely not blocked, so reporting one would be a false positive.
    Both fields come from the crawl and are only written where a restriction exists, so
    a repo with no restriction yields no finding rather than an unmeasured pass.
    """
    allowed = repo.get("_branch_protection_push_restrictions")
    if allowed is None:
        return []
    allowed_teams = set(allowed.get("teams") or [])
    enforce_admins = repo.get("_branch_protection_enforce_admins")
    return sorted(
        team
        for team, perm in (repo.get("teams") or {}).items()
        if perm in MERGE_CAPABLE_PERMISSIONS
        and team not in allowed_teams
        and (enforce_admins or perm != "admin")
    )


def _push_allow_list(repo: dict[str, Any]) -> str:
    """Render the push allow-list, so a SEC-16 finding names who IS still allowed."""
    allowed = repo.get("_branch_protection_push_restrictions") or {}
    users = ",".join(allowed.get("users") or []) or "-"
    teams = ",".join(allowed.get("teams") or []) or "-"
    return f"push restricted to users={users} teams={teams}"


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
        "default branch has no ruleset and no branch protection"
        " (unmanaged tier exempt)",
        lambda r: (
            (
                "unprotected",
                "covered by an org ruleset",
                "set `tier` so a ruleset matches",
            )
            if not r.get("_has_branch_protection")
            and not r.get("_ruleset_count")
            and r.get("tier") != "unmanaged"
            else None
        ),
    ),
    Rule(
        "SEC-04",
        "security",
        "high",
        "active",
        "secret scanning or push protection disabled",
        # ACCEPTED RISK on the 10 active private repos (decision 2026-08-17, closing
        # SEC-04/13): enabling secret scanning on a PRIVATE repo spends a paid "Secret
        # Protection" seat (`maximum_advanced_security_committers`, capped at 6 on the
        # org's Team plan; public-repo scanning is free and does not count against it).
        # Buying more seats was the blocker (see tk-sec-04-13 in the workflow project);
        # the org has decided not to purchase them, so `secret_scanning: disabled`
        # stays the deliberate value on `hq`, `access-forge`, `alerting-omnibus`,
        # `apisix-testbed`, `common-access`, `concourse-workflow`, `gwarek`,
        # `oldevops-scratch`, `open-collaboration`, `product` -- see each repo's YAML.
        # Left firing rather than exempted: unlike SEC-01's fork exemption, this IS a
        # real, standing risk on those 10 repos, just a knowingly accepted one -- the
        # audit should keep naming it rather than going quiet.
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
    # SEC-05 (Dependabot security updates disabled) is RETIRED, not merely quiet.
    # Closed 2026-08-14 as won't-fix: the org's shared Renovate config
    # (`mitodl/.github:renovate-config`) already sets
    # `vulnerabilityAlerts.enabled: true` plus `osvVulnerabilityAlerts: true`, sourced
    # from the same `vulnerability_alerts` data GitHub's own toggle would use,
    # bypassing Renovate's normal schedule. Enabling
    # GitHub's native auto-fix PRs on top of that would duplicate Renovate's PR on every
    # repo extending the shared config -- see the `base` archetype's comment in
    # data/archetypes.yaml. A rule left in place here would report that duplication risk
    # as a "disabled" finding on 174 of 176 active repos, forever, which is the opposite
    # of what actually happened: the archetype default is `false` on purpose now.
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
        "SEC-16",
        "security",
        "high",
        "active",
        "classic push restriction excludes a team that holds a merge-capable grant",
        # Graded `high` on ACCESS grounds rather than exposure: it does not weaken the
        # repo, it silently revokes people's ability to ship to it, and it is invisible
        # in every other field. An unannounced loss of access is a security finding in
        # the same sense an unannounced grant is -- the declared model and the enforced
        # one disagree, and nobody can tell which one is live.
        lambda r: (
            (
                f"{_push_allow_list(r)}; "
                f"blocked: {', '.join(_blocked_by_push_restriction(r))}",
                "every team with push or better can merge",
                "drop the push restriction (org rulesets already cover this branch), "
                "or add the blocked teams to its allow-list",
            )
            if _blocked_by_push_restriction(r)
            else None
        ),
    ),
    Rule(
        "CON-12",
        "security",
        "high",
        "active",
        "no team grants -- reachable only by an org owner or a direct grant",
        # The org's `default_repository_permission` is `none` (verified 2026-08-10),
        # so org membership confers NOTHING. A repo with an empty `teams` block is
        # therefore genuinely unreachable except by the nine org owners, who hold
        # implicit admin, and by whoever holds a direct collaborator grant.
        #
        # THIS ONLY BECAME A FINDING WHEN THE DEFAULT WAS CHECKED. Under a `read` or
        # `write` org default the same data would be cosmetic -- every member would
        # already have access and an empty `teams` block would mean nothing. Whoever
        # revisits this rule should re-check the org default first, because it
        # silently decides whether the rule is measuring anything at all.
        lambda r: (
            (
                "no team grants",
                "at least one team grant",
                "add the archetype's team block, or record why this repo is exempt",
            )
            if not (r.get("teams") or {})
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


#: Why a direct collaborator grant exists, which is what decides how to remove it.
def fetch_rosters() -> tuple[
    dict[str, set[str]], set[str], dict[str, str | None], set[str]
]:
    """Team rosters, org members and team nesting, read live from the API.

    NOT read from committed data, and not cached to disk, because there is nowhere
    safe to put it: `vault-developer-access` and `vault-devops-access` are declared
    `privacy: secret` in organization/teams.py so their membership is not advertised
    even inside the org, and ol-infrastructure is a PUBLIC repository. Committing
    rosters here would publish exactly what that setting exists to withhold.

    Shared by `bin/github-estate-audit access` and `bin/github-collaborator-cleanup`
    -- both need a roster snapshot no older than "right now" (§ SEC-06: a roster that
    changed between audit and action turns a `redundant` grant into `no-access`, and
    acting on the stale classification silently revokes someone's only path in).
    """
    import httpx  # noqa: PLC0415 -- only roster-fetching callers need the API

    from ol_infrastructure.lib.github_helper import (  # noqa: PLC0415
        API_HEADERS,
        GITHUB_API,
        get_installation_token,
    )

    org = "mitodl"
    token = get_installation_token()

    def paginate(client: httpx.Client, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            response = client.get(url)
            response.raise_for_status()
            out.extend(response.json())
            url = response.links.get("next", {}).get("url")
        return out

    with httpx.Client(
        base_url=GITHUB_API,
        headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        timeout=60,
        follow_redirects=True,
    ) as client:
        members = {m["login"] for m in paginate(client, f"/orgs/{org}/members")}
        # Org OWNERS hold implicit admin on every repo -- a third access path beside
        # teams and direct grants, and the only one the others cannot revoke. The
        # plain members list does not distinguish them, so this is a separate call.
        owners = {
            m["login"] for m in paginate(client, f"/orgs/{org}/members?role=admin")
        }
        teams = paginate(client, f"/orgs/{org}/teams")
        rosters = {
            t["slug"]: {
                m["login"]
                for m in paginate(client, f"/orgs/{org}/teams/{t['slug']}/members")
            }
            for t in teams
        }
    parents = {t["slug"]: (t.get("parent") or {}).get("slug") for t in teams}
    return rosters, members, parents, owners


GrantKind = Literal["redundant", "level-only", "owner-implicit", "no-access", "outside"]
#: The subset of `GrantKind` a caller may safely act on with a delete. Kept as its own
#: `Literal` (not just a runtime check against `REMOVABLE_KINDS`) so a CLI built on top
#: -- `bin/github-collaborator-cleanup`'s `--kind` -- gets this enforced at argument
#: parsing, before any classification or API call runs. `RemovableKind` and
#: `REMOVABLE_KINDS` must be kept in sync; there is no single source of truth to
#: generate one from the other because a `Literal`'s members are not introspectable
#: from a frozenset at the type-checker level.
RemovableKind = Literal["redundant", "level-only", "owner-implicit"]
#: The kinds whose removal costs the person nothing. `no-access` and `outside` are
#: deliberately absent: one revokes access, the other is a membership decision.
REMOVABLE_KINDS: frozenset[str] = frozenset(
    {"redundant", "level-only", "owner-implicit"}
)


def _team_members(rosters: dict[str, set[str]], parents: dict[str, str | None]) -> Any:
    """Expand each team to include the members of its descendant teams.

    Nested teams inherit DOWNWARD on GitHub: a member of a child team counts as a
    member of the parent for access purposes, so a grant to `odl-engineering` also
    covers everyone in `copilot`, its child. Matching slugs exactly would classify
    those people as having no team access and manufacture `no-access` findings.
    """
    expanded = {slug: set(members) for slug, members in rosters.items()}
    for slug, members in rosters.items():
        parent = parents.get(slug)
        while parent:
            expanded.setdefault(parent, set()).update(members)
            parent = parents.get(parent)
    return expanded


def classify_direct_grants(
    fleet: Iterable[dict[str, Any]],
    rosters: dict[str, set[str]],
    members: set[str],
    parents: dict[str, str | None] | None = None,
    owners: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Explain every direct collaborator grant in terms of how to remove it.

    A flat count of direct grants (SEC-06) says how much there is to clean up but
    nothing about how, and the five kinds need completely different handling:

      redundant       the person's team access already meets or beats the direct
                      grant. Delete it; nobody loses anything.
      level-only      they keep team access, just at a lower rung. Since that lower
                      rung IS the SEC-15 target, deleting is the intended outcome.
      owner-implicit  the person is an ORG OWNER, so admin on every repo survives
                      the deletion regardless of teams. Free to delete.
      no-access       the direct grant is their only path in. Deleting it without a
                      team grant first REVOKES ACCESS -- these gate the cleanup.
      outside         not an org member at all. A different decision entirely:
                      invite them to the org and a team, or remove them.

    OWNERSHIP IS CHECKED BEFORE TEAMS, and that ordering is the point. Org ownership
    is a third access path alongside teams and direct grants, and it is the one the
    other two cannot take away: an owner keeps implicit admin no matter what happens
    to a roster. Ranking a grant by teams alone put 10 owner-held grants in
    `no-access` -- the bucket that means "removing this revokes access" -- and so
    overstated the gating set by nearly half. `owner-implicit` also survives roster
    churn in a way `redundant` does not, which is why owners get their own bucket
    rather than being folded into it.

    `rosters` and `owners` are passed in rather than read from disk because team
    membership is NOT committed: `vault-developer-access` and `vault-devops-access`
    are `privacy: secret` precisely so membership is not advertised, and
    ol-infrastructure is a PUBLIC repository. Callers fetch them live -- which is why
    the `access` command needs credentials while `run` does not.
    """
    expanded = _team_members(rosters, parents or {})
    rows: list[dict[str, Any]] = []
    for repo in fleet:
        grants = repo.get("teams") or {}
        for login, role in (repo.get("_direct_collaborators") or {}).items():
            via = max(
                (
                    PERMISSION_RANK[perm]
                    for team, perm in grants.items()
                    if login in expanded.get(team, ())
                ),
                default=None,
            )
            if login not in members:
                kind: GrantKind = "outside"
            elif login in (owners or set()):
                kind = "owner-implicit"
            elif via is None:
                kind = "no-access"
            elif via >= PERMISSION_RANK[role]:
                kind = "redundant"
            else:
                kind = "level-only"
            rows.append(
                {
                    "repo": repo["name"],
                    "archived": bool(repo.get("archived")),
                    "login": login,
                    "role": role,
                    "via_teams": RANK_NAME[via] if via is not None else None,
                    "kind": kind,
                }
            )
    return rows


def grant_shapes(
    fleet: Iterable[dict[str, Any]],
) -> Counter[tuple[tuple[str, str], ...]]:
    """Count the distinct team-grant shapes in `fleet`.

    One shape per distinct (team, permission) set. The number of shapes IS the
    uniformity metric: 176 repos drawn from 4 shapes is a model, the same 176 drawn
    from 27 is an accretion of one-off decisions.
    """
    return Counter(tuple(sorted((repo.get("teams") or {}).items())) for repo in fleet)
