"""Load the fleet data and resolve each repo against its archetype.

This is the Pulumi-side reader for the same files `bin/github-org-inventory` writes.
Both use the identical resolution rules, which is what makes the empty-diff gate
reachable -- if they disagreed, the code would declare something the crawl never
recorded.
"""

import re
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).parent / "data"
REPOS_DIR = DATA_DIR / "repos"

#: Team slug -> numeric id, written by `bin/github-org-inventory crawl`. Required
#: because `TeamRepository` state records the numeric id. See repository.py.
TEAM_IDS: dict[str, int] = yaml.safe_load((DATA_DIR / "teams.yaml").read_text())


def _resolve(archetypes: dict[str, Any], name: str) -> dict[str, Any]:
    """Flatten an archetype's `extends` chain.

    A key set to `null` in a child means EXPLICITLY NOT ENFORCED and is dropped
    rather than inherited -- `fork` uses this to opt out of `default_branch`.
    See plan section 3.4.
    """
    spec = dict(archetypes[name])
    parent = spec.pop("extends", None)
    if not parent:
        return {k: v for k, v in spec.items() if v is not None}
    merged = _resolve(archetypes, parent)
    merged.update(spec)
    return {k: v for k, v in merged.items() if v is not None}


#: What `github.TeamRepository(permission=...)` accepts. NOT the same vocabulary the
#: collaborator API uses -- GitHub says `pull`/`push` for teams and `read`/`write` for
#: collaborators, for the same two rungs. Writing `read` in a `teams:` block is
#: therefore wrong even though it reads as valid English, and the provider rejects it
#: at apply time, several hundred resources into a run.
TEAM_PERMISSIONS = frozenset({"pull", "triage", "push", "maintain", "admin"})
#: What `GET /repos/{owner}/{repo}/collaborators` reports as `role_name`, and so what
#: `_direct_collaborators` holds. The audit ranks these against team grants.
COLLABORATOR_ROLES = frozenset({"read", "triage", "write", "maintain", "admin"})


def _check_permission_values(fleet: list[dict[str, Any]]) -> None:
    """Fail if any grant carries a permission outside its field's vocabulary.

    Two consumers index these values unguarded, and both should. `repository.py`
    passes `permission` straight to `TeamRepository`, and `audit.classify_direct_grants`
    indexes `PERMISSION_RANK[...]` for both fields.

    THE `.get()` FIX WOULD BE WORSE THAN THE CRASH, and specifically worse here. A role
    that silently ranks as `None` or 0 makes the audit classify a direct grant as
    `redundant` -- the bucket whose whole meaning is "safe to delete, the person keeps
    team access". Acting on that revokes someone's access on the strength of a typo.
    Misclassifying access is the one outcome worse than failing to classify it.

    So this keeps the loud failure and adds what a bare KeyError lacks: which repo,
    which field, which value, and every offender in one run rather than the first one
    encountered several hundred repos into a preview. Same reasoning as
    `_check_team_references` above.
    """
    bad = sorted(
        {
            f"{repo['name']}: teams.{slug} = {perm!r}"
            for repo in fleet
            for slug, perm in (repo.get("teams") or {}).items()
            if perm not in TEAM_PERMISSIONS
        }
        | {
            f"{repo['name']}: _direct_collaborators.{login} = {role!r}"
            for repo in fleet
            for login, role in (repo.get("_direct_collaborators") or {}).items()
            if role not in COLLABORATOR_ROLES
        }
    )
    if bad:
        message = (
            "fleet data carries permission values outside the allowed vocabulary:\n  "
            + "\n  ".join(bad)
            + f"\nTeam grants must be one of {sorted(TEAM_PERMISSIONS)}; "
            f"direct collaborator roles one of {sorted(COLLABORATOR_ROLES)}. "
            "Note `pull`/`push` for teams vs `read`/`write` for collaborators -- "
            "GitHub uses different words for the same two rungs."
        )
        raise ValueError(message)


#: Every PUBLIC repo grants these, per policy 2026-08-10. Enforced rather than
#: documented, because the failure is silent and expensive: `teams` REPLACES an
#: archetype's grants wholesale, so a repo file that declares its own block and omits
#: these revokes the whole engineering organisation with no error anywhere.
REQUIRED_PUBLIC_TEAMS = frozenset({"odl-engineering", "odl-engineering-owners"})


def _check_public_repo_teams(fleet: list[dict[str, Any]]) -> None:
    """Fail if an active public repo does not grant the two required teams.

    Scoped to ACTIVE PUBLIC repos, matching the policy exactly:

      archived  repository.py emits no TeamRepository for them, so there is nothing
                to enforce and a failure here would be unfixable.
      private   deliberately exempt. `access-forge` and `gwarek` are devops-only
                because that is the intent, not an oversight.

    VISIBILITY IS REQUIRED, NOT ASSUMED. A repo with no visibility signal cannot be
    checked, and silently skipping it is the failure mode this project keeps hitting --
    unmeasured and compliant look identical. `_visibility` is written on every repo by
    the crawl and `visibility` comes from the archetype, so an active repo missing both
    is hand-authored data that has to say which it is.
    """
    unknown: list[str] = []
    missing: list[str] = []
    for repo in fleet:
        if repo.get("archived"):
            continue
        visibility = repo.get("_visibility") or repo.get("visibility")
        if not visibility:
            unknown.append(repo["name"])
            continue
        if visibility != "public":
            continue
        absent = REQUIRED_PUBLIC_TEAMS - set(repo.get("teams") or {})
        if absent:
            missing.append(f"{repo['name']}: missing {sorted(absent)}")
    if unknown:
        message = (
            "active repos with no visibility recorded, so the public-team policy "
            "cannot be checked:\n  "
            + "\n  ".join(sorted(unknown))
            + "\nAdd `_visibility` (or `visibility`), or re-run "
            "`bin/github-org-inventory crawl --refresh`."
        )
        raise ValueError(message)
    if missing:
        message = (
            "public repos must grant "
            f"{sorted(REQUIRED_PUBLIC_TEAMS)} (policy 2026-08-10):\n  "
            + "\n  ".join(sorted(missing))
            + "\n`teams` REPLACES the archetype's grants rather than merging into "
            "them, so a repo declaring its own block must restate both."
        )
        raise ValueError(message)


def _check_dependabot_requires_alerts(fleet: list[dict[str, Any]]) -> None:
    """Fail if a repo asks for Dependabot security updates with alerts off.

    GitHub refuses that combination outright -- `/automated-security-fixes` answers 422
    unless vulnerability alerts are enabled -- so it is unsatisfiable data, not a
    preference Pulumi could honour.

    This exists because `repository.py` now SKIPS `RepositoryDependabotSecurityUpdates`
    entirely on an alerts-off repo (see the comment there for why declaring the OFF
    state broke the 2026-08-17 deploy). That skip is correct for every combination the
    fleet actually holds, but it would swallow this one: a repo asking for updates it
    cannot have would simply get no resource and no complaint. Failing here keeps the
    skip from turning an impossible request into a silent one.
    """
    conflicting = sorted(
        repo["name"]
        for repo in fleet
        if not repo.get("archived")
        and repo.get("dependabot_security_updates")
        and not repo.get("vulnerability_alerts")
    )
    if conflicting:
        message = (
            "repos request dependabot_security_updates with vulnerability_alerts "
            "disabled, which GitHub rejects (422):\n  "
            + "\n  ".join(conflicting)
            + "\nEnable vulnerability_alerts on these repos, or drop the "
            "dependabot_security_updates request."
        )
        raise ValueError(message)


#: A check name ending in `(...)` is one cell of a job matrix -- see rulesets.py.
_MATRIX_SHARD = re.compile(r"\(.+\)$")

#: Per-repo opt-in for requiring matrix shard names.
#: See `_check_required_status_checks`.
MATRIX_OPT_IN = "_allow_matrix_shard_checks"


def _check_required_status_checks(fleet: list[dict[str, Any]]) -> None:
    """Fail on required-check data that would block every PR on a repo.

    Unlike the other checks here, this one is not guarding against a crash further down.
    `rulesets.py` will happily build a ruleset from any list of strings, and GitHub will
    accept it. The failure is silent and total: a context nothing produces leaves every
    PR permanently pending, and because `enforcement: evaluate` does not exist on this
    plan there is no state between "not enforced" and "enforced on the org's busiest
    repos". Data that is wrong in an obvious way should not reach that point.

    Three things are refused:

    archived repos  GitHub rejects ruleset writes on them, so it is unsatisfiable data.
    empty strings   an empty context blocks the branch and names nothing in the UI.
    matrix shards   `python-tests (1)` is a valid context and requiring it works
                    today.
                    What it does not survive is somebody editing that matrix in a
                    DIFFERENT repository: drop to three shards and `python-tests (4)` is
                    still required, never produced again, and every PR on that repo
                    blocks forever with the cause two repos away. Requiring one is
                    allowed, but only from a repo file that sets
                    `_allow_matrix_shard_checks: true`, so it is a decision somebody
                    made rather than a name copied out of a
                    `bin/github-required-checks sample` run.
    """
    problems: list[str] = []
    for repo in fleet:
        contexts = repo.get("required_status_checks")
        if not contexts:
            continue
        name = repo["name"]
        if repo.get("archived"):
            problems.append(f"{name}: archived repos cannot carry rulesets")
            continue
        if not isinstance(contexts, list) or any(
            not isinstance(c, str) or not c.strip() for c in contexts
        ):
            problems.append(
                f"{name}: required_status_checks must be a list of non-empty strings"
            )
            continue
        if not repo.get(MATRIX_OPT_IN):
            shards = sorted(c for c in contexts if _MATRIX_SHARD.search(c))
            if shards:
                problems.append(
                    f"{name}: {shards} look like job-matrix shard names; set "
                    f"`{MATRIX_OPT_IN}: true` to require them anyway"
                )
    if problems:
        message = (
            "required_status_checks data would block merges:\n  "
            + "\n  ".join(problems)
            + "\nVerify every context against real check runs first: "
            "`uv run bin/github-required-checks sample <repo>`, and that no open PR is "
            "left unable to produce it: `uv run bin/github-required-checks blocked`. "
            "Only a name that tool marks SAFE may be required -- there is no dry-run "
            "enforcement mode on this plan."
        )
        raise ValueError(message)


def _check_protected_release_branches(fleet: list[dict[str, Any]]) -> None:
    """Fail on `protected_release_branches` data GitHub would reject or that lies.

    Same shape of check as `_check_required_status_checks`: archived repos can't
    carry rulesets, and a non-string/empty entry would build a ruleset that
    matches nothing while still claiming (via `_ruleset_count`-style tooling) that
    the branch is protected.
    """
    problems: list[str] = []
    for repo in fleet:
        branches = repo.get("protected_release_branches")
        if not branches:
            continue
        name = repo["name"]
        if repo.get("archived"):
            problems.append(f"{name}: archived repos cannot carry rulesets")
            continue
        if not isinstance(branches, list) or any(
            not isinstance(b, str) or not b.strip() for b in branches
        ):
            problems.append(
                f"{name}: protected_release_branches must be a list of "
                "non-empty strings"
            )
    if problems:
        message = (
            "fleet data carries invalid protected_release_branches:\n  "
            + "\n  ".join(problems)
        )
        raise ValueError(message)


def _check_team_references(fleet: list[dict[str, Any]]) -> None:
    """Fail if any repo grants to a team slug absent from teams.yaml.

    repository.py looks up `TEAM_IDS[team_slug]` unguarded, and must: the numeric id
    is required for a non-destructive TeamRepository, so an unknown slug is data we
    cannot act on. The defensive-looking `.get()` would be a REGRESSION -- it yields
    `team_id="None"` and Pulumi then plans a grant against a nonexistent team.

    What a bare KeyError lacks is provenance. It names the slug but not the repo that
    referenced it, and surfaces mid-preview several hundred repos in. Checking here
    reports every offender in one run, before any resource is constructed.

    Grants come from the merged dict because archetypes carry `teams` too, so a bad
    slug in archetypes.yaml would fan out across every repo that extends it.
    """
    unknown = sorted(
        {
            f"{repo['name']}: {slug!r}"
            for repo in fleet
            for slug in (repo.get("teams") or {})
            if slug not in TEAM_IDS
        }
    )
    if unknown:
        message = (
            "fleet data grants to teams that are not in teams.yaml:\n  "
            + "\n  ".join(unknown)
            + "\nIf a team was renamed or created, re-run "
            "`bin/github-org-inventory crawl` to refresh teams.yaml."
        )
        raise ValueError(message)


def load_fleet() -> list[dict[str, Any]]:
    """Return one merged dict per repo: archetype defaults under its own values.

    MUST use pathlib rather than glob.glob or a shell glob. The org's `.github`
    repo lands at `repos/.github.yaml`, a dotfile, which `glob.glob("*.yaml")`
    silently skips -- 315 instead of 316. A repo missing from the fleet looks
    exactly like one nobody has gotten to yet. See data/README.md.
    """
    raw = yaml.safe_load((DATA_DIR / "archetypes.yaml").read_text())
    archetypes = raw["archetypes"]
    effective = {name: _resolve(archetypes, name) for name in archetypes}

    fleet: list[dict[str, Any]] = []
    for path in sorted(REPOS_DIR.glob("*.yaml")):
        declared = yaml.safe_load(path.read_text())
        archetype = declared["archetype"]
        if archetype not in effective:
            message = f"{path.name} names archetype {archetype!r}, which is not defined"
            raise ValueError(message)
        merged = {**effective[archetype], **declared}
        fleet.append(merged)

    _check_team_references(fleet)
    _check_permission_values(fleet)
    _check_public_repo_teams(fleet)
    _check_dependabot_requires_alerts(fleet)
    _check_required_status_checks(fleet)
    _check_protected_release_branches(fleet)

    # The dotfile trap is silent by construction, so assert rather than trust.
    assignments = yaml.safe_load((DATA_DIR / "archetypes-proposed.yaml").read_text())
    expected = sum(len(names) for names in assignments.values())
    if len(fleet) != expected:
        found = {repo["name"] for repo in fleet}
        wanted = {n for names in assignments.values() for n in names}
        message = (
            f"loaded {len(fleet)} repo files but the assignment file lists "
            f"{expected}. Missing: {sorted(wanted - found)}. A dotfile-unsafe "
            f"glob is the usual cause -- see data/README.md."
        )
        raise ValueError(message)
    return fleet
