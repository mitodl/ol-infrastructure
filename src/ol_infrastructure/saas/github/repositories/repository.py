"""The per-repo resource factory.

One function, called once per repo in the fleet. Which resources it emits depends
on whether the repo is archived: GitHub rejects most writes to an archived repo, so
those get a `Repository` and nothing else (plan section 4.4).

WHAT IS DELIBERATELY NOT EMITTED HERE, and why it is not an oversight:

  RepositoryWebhook, BranchProtection, RepositoryRuleset, RepositoryDeployKey
      The inventory records these exist -- ids, counts, ruleset names -- but not
      the fields Pulumi needs to declare them: webhook `events` and
      `content_type`, the full branch-protection object, ruleset rules and bypass
      actors, deploy-key material. Declaring them from partial data would import
      resources that immediately diff, which is worse than not importing them:
      an unmanaged resource is visibly absent, while a permanently-diffing one
      trains everyone to ignore the gate. They need a crawl extension first.

  ActionsSecret / DependabotSecret
      Values are never returned, so an import leaves them unset and the next
      preview wants to write one (section 4.1).

  RepositoryEnvironment
      7,803 of them, overwhelmingly ephemeral review apps. Allowlist only
      (section 4.3), and no repo has declared an allowlist yet.

  RepositoryCollaborator
      There should eventually be none at all (section 4.7).
"""

from typing import Any

import pulumi_github as github
from pulumi import ResourceOptions

from ol_infrastructure.saas.github.repositories import archetypes
from ol_infrastructure.saas.github.tiers import TIER_PROPERTY_NAME


def _security_and_analysis(
    repo: dict[str, Any],
) -> github.RepositorySecurityAndAnalysisArgs | None:
    """Mirror the live security-and-analysis block.

    GitHub rejects this block entirely on private repos that lack Advanced
    Security, and reports it as absent on archived repos, so it is only set where
    the crawl actually observed a status.
    """
    scanning = repo.get("secret_scanning")
    push_protection = repo.get("secret_scanning_push_protection")
    if scanning is None and push_protection is None:
        return None
    return github.RepositorySecurityAndAnalysisArgs(
        secret_scanning=github.RepositorySecurityAndAnalysisSecretScanningArgs(
            status=scanning or "disabled"
        ),
        secret_scanning_push_protection=(
            github.RepositorySecurityAndAnalysisSecretScanningPushProtectionArgs(
                status=push_protection or "disabled"
            )
        ),
    )


#: Settings GitHub will not let Pulumi write on an archived repo. It also omits most
#: of them from the API response, so an import records nothing and any declaration
#: reads as an addition -- an update that would fail on apply. Ignoring them is the
#: honest statement: the repo is read-only and these are not ours to manage.
#: `template` records which template repo this one was generated from. It is
#: create-time provenance, not configuration -- GitHub never lets it change and we
#: never set it -- but the import records it, so leaving it undeclared reads as a
#: removal. ol-keycloakify is the one repo in the fleet that has it.
_ALWAYS_IGNORED = ["template", "isTemplate"]

_ARCHIVED_IGNORED_SETTINGS = [
    *_ALWAYS_IGNORED,
    "allowAutoMerge",
    "deleteBranchOnMerge",
    "topics",
    "visibility",
    "allowMergeCommit",
    "allowRebaseMerge",
    "allowSquashMerge",
    "deleteBranchOnMerge",
    "description",
    "hasDiscussions",
    "hasIssues",
    "hasProjects",
    "hasWiki",
    "homepageUrl",
    "mergeCommitMessage",
    "mergeCommitTitle",
    "securityAndAnalysis",
    "squashMergeCommitMessage",
    "squashMergeCommitTitle",
    "webCommitSignoffRequired",
]


def build(repo: dict[str, Any]) -> None:
    """Emit the resource family for one repo."""
    name = repo["name"]
    archived = bool(repo.get("archived"))

    if archived:
        # Almost empty on purpose (section 4.4). Archived repos are imported so that
        # nothing stops someone re-adding one later with a full config and triggering
        # a wave of failed writes -- but what is managed is the archived flag and the
        # name, nothing more.
        github.Repository(
            f"mitodl-repo-{name}",
            name=name,
            archived=True,
            visibility=repo.get("visibility"),
            opts=ResourceOptions(
                retain_on_delete=True,
                ignore_changes=_ARCHIVED_IGNORED_SETTINGS,
            ),
        )
        return

    # retain_on_delete is the counterweight for administration:write (section 2.2).
    # Removing a repo from data/repos/ drops it from Pulumi state and NEVER from
    # GitHub. Every other safety property here is downstream of this one.
    repository = github.Repository(
        f"mitodl-repo-{name}",
        name=name,
        description=repo.get("description"),
        homepage_url=repo.get("homepage"),
        visibility=repo.get("visibility"),
        archived=False,
        has_issues=repo.get("has_issues"),
        has_wiki=repo.get("has_wiki"),
        allow_squash_merge=repo.get("allow_squash_merge"),
        allow_merge_commit=repo.get("allow_merge_commit"),
        allow_rebase_merge=repo.get("allow_rebase_merge"),
        allow_auto_merge=repo.get("allow_auto_merge"),
        delete_branch_on_merge=repo.get("delete_branch_on_merge"),
        web_commit_signoff_required=repo.get("web_commit_signoff_required"),
        has_discussions=repo.get("has_discussions"),
        has_projects=repo.get("has_projects"),
        squash_merge_commit_title=repo.get("squash_merge_commit_title"),
        squash_merge_commit_message=repo.get("squash_merge_commit_message"),
        merge_commit_title=repo.get("merge_commit_title"),
        merge_commit_message=repo.get("merge_commit_message"),
        # NOT `vulnerability_alerts=` -- that input is deprecated in favour of the
        # RepositoryVulnerabilityAlerts resource below, and setting both makes the
        # provider emit a deprecation warning on every preview for 316 repos.
        security_and_analysis=_security_and_analysis(repo),
        opts=ResourceOptions(
            retain_on_delete=True,
            ignore_changes=_ALWAYS_IGNORED,
        ),
    )

    if repo.get("topics"):
        github.RepositoryTopics(
            f"mitodl-repo-topics-{name}",
            repository=name,
            topics=repo["topics"],
            opts=ResourceOptions(retain_on_delete=True),
        )

    github.BranchDefault(
        f"mitodl-repo-default-branch-{name}",
        repository=name,
        branch=repo["default_branch"],
        opts=ResourceOptions(depends_on=[repository]),
    )

    github.RepositoryVulnerabilityAlerts(
        f"mitodl-repo-vulnerability-alerts-{name}",
        repository=name,
        enabled=bool(repo.get("vulnerability_alerts")),
        opts=ResourceOptions(depends_on=[repository]),
    )

    # The tier value. This is a CREATE, not an import -- no repo carries a custom
    # property today. Until it lands, every repo inherits `standard` from the
    # property default, which is why phase 3.5's rulesets must come after this
    # (see organization/custom_properties.py).
    github.RepositoryCustomProperty(
        f"mitodl-repo-tier-{name}",
        repository=name,
        property_name=TIER_PROPERTY_NAME,
        property_type="single_select",
        property_values=[repo["tier"]],
        opts=ResourceOptions(depends_on=[repository]),
    )

    for team_slug, permission in (repo.get("teams") or {}).items():
        github.TeamRepository(
            f"mitodl-team-repo-{team_slug}-{name}",
            repository=name,
            # MUST be the numeric id, not the slug. The provider accepts a slug on
            # create -- which is what plan section 3.1 assumed -- but imported state
            # records the numeric id, so declaring the slug reads as a change to an
            # immutable field and Pulumi plans a REPLACE. Replacing a TeamRepository
            # revokes and re-grants a team's access to a repo, so the assumption was
            # not merely untidy, it was destructive. Ids come from data/teams.yaml,
            # written by the same crawl, so no StackReference is needed either way.
            team_id=str(archetypes.TEAM_IDS[team_slug]),
            permission=permission,
            opts=ResourceOptions(depends_on=[repository]),
        )
