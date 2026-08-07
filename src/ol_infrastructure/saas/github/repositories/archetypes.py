"""Load the fleet data and resolve each repo against its archetype.

This is the Pulumi-side reader for the same files `bin/github-org-inventory` writes.
Both use the identical resolution rules, which is what makes the empty-diff gate
reachable -- if they disagreed, the code would declare something the crawl never
recorded.
"""

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
