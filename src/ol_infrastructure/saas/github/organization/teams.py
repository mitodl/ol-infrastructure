"""The 12 mitodl teams, as data plus a loop.

Teams are declared here; team ROSTERS are not (plan section 4.7). Pulumi manages
which teams exist and -- in the repositories project -- what each team can do. It
does not manage who is in one, so onboarding, offboarding and team moves never touch
this repository, and the 39/39 seat ceiling is not a Pulumi concern.

Nesting is expressed by `parent`. Declaration order below does not matter: the dict
is resolved parents-first before any resource is constructed, so a child may name a
parent that appears later in the file.
"""

from typing import Any

import pulumi_github as github
from pulumi import ResourceOptions

# Read off `GET /orgs/mitodl/teams` on 2026-08-05. `notification_setting` is
# `notifications_enabled` on all 14, so the loop sets it once rather than repeating it.
TEAMS: dict[str, dict[str, Any]] = {
    "arbisoft-contractors": {
        "name": "Arbisoft Contractors",
        "description": "software engineers from Arbisoft or Edly",
        "privacy": "closed",
    },
    # `code-owners` and `code-owners-mitx-online` DELETED 2026-08-17. They existed to
    # back `@mitodl/code-owners*` entries in CODEOWNERS files, and no CODEOWNERS file
    # in the org referenced either one -- nor did any branch protection require code
    # owner review, so even a stale reference would have been inert. What they did
    # instead was launder permissions: both nested under `odl-engineering-owners`, so
    # membership conferred inherited admin on ~190 repos while the team's own name
    # suggested a review role. `/orgs/.../teams/{slug}/repos` reports that inherited
    # admin indistinguishably from a direct grant, which is how it stayed unexamined.
    #
    # Deleting cost nobody access: all five members of `code-owners` and all three of
    # `code-owners-mitx-online` are already DIRECT members of `odl-engineering-owners`.
    # Verified before deletion rather than assumed -- a child team's members do not
    # automatically belong to its parent, so this had to be checked per person.
    "copilot": {
        "name": "copilot",
        "description": "users with copilot seats",
        "privacy": "closed",
        "parent": "odl-engineering",
    },
    "devops": {
        "name": "DevOps",
        "description": "",
        "privacy": "closed",
    },
    "devops-contractors": {
        "name": "DevOps Contractors",
        "description": "",
        "privacy": "closed",
        "parent": "devops",
    },
    # NOTE: 'Enginineering' is the live value, typo and all.
    # Recording reality is the point (section 6); fix it in phase 5.
    "odl-engineering": {
        "name": "odl-engineering",
        "description": "ODL Enginineering",
        "privacy": "closed",
    },
    # NOTE: `None`, not `""`. The API returns JSON null here, whereas devops,
    # devops-contractors and odlengweb genuinely hold empty strings. Both forms
    # preview clean (verified), so this is a fidelity choice: `""` would assert an
    # empty description where GitHub reports absence.
    "odl-engineering-owners": {
        "name": "ODL Engineering Owners",
        "description": None,
        "privacy": "closed",
    },
    "odlengweb": {
        "name": "odlengweb",
        "description": "",
        "privacy": "closed",
    },
    "ol-data": {
        "name": "ol-data",
        "description": (
            "Engineers and product owners working on data related projects "
            "and platform engineering"
        ),
        "privacy": "closed",
    },
    # NOTE: the live description has a trailing space. Preserved so the
    # empty-diff gate holds; strip it in phase 5 if anyone cares.
    "owners-mit-learn": {
        "name": "Owners MIT Learn",
        "description": "code owners for the MIT Learn product ",
        "privacy": "closed",
    },
    "owners-mit-open": {
        "name": "Owners MIT Open",
        "description": "code owners for the MIT Open product",
        "privacy": "closed",
    },
    # `secret` rather than `closed`: these gate Vault access and their
    # membership is not advertised to the rest of the org.
    "vault-developer-access": {
        "name": "vault-developer-access",
        "description": (
            "Members of this team will have some access to Vault QA secrets"
        ),
        "privacy": "secret",
    },
    "vault-devops-access": {
        "name": "vault-devops-access",
        "description": (
            "Members of this team will have unrestricted access to Vault QA secrets"
        ),
        "privacy": "secret",
    },
}


def _in_parent_order(teams: dict[str, dict[str, Any]]) -> list[str]:
    """Return slugs ordered so a parent always precedes its children."""
    ordered: list[str] = []
    while len(ordered) < len(teams):
        progressed = False
        for slug, spec in teams.items():
            if slug in ordered:
                continue
            parent = spec.get("parent")
            if parent is None or parent in ordered:
                ordered.append(slug)
                progressed = True
        if not progressed:
            unresolved = sorted(set(teams) - set(ordered))
            message = f"cycle or unknown parent among teams: {unresolved}"
            raise ValueError(message)
    return ordered


github_teams: dict[str, github.Team] = {}
for slug in _in_parent_order(TEAMS):
    spec = TEAMS[slug]
    parent_slug = spec.get("parent")
    github_teams[slug] = github.Team(
        f"mitodl-team-{slug}",
        name=spec["name"],
        description=spec.get("description"),
        privacy=spec["privacy"],
        # Uniform across all 12 today; move into TEAMS if that stops being true.
        notification_setting="notifications_enabled",
        # `parent_team_id` accepts a slug as well as a numeric id, so nesting needs
        # no output plumbing. depends_on still has to be explicit: a plain string
        # reference creates no implicit dependency for Pulumi to order on.
        parent_team_id=parent_slug,
        opts=ResourceOptions(
            protect=True,
            depends_on=[github_teams[parent_slug]] if parent_slug else [],
        ),
    )
