"""Organization-wide settings for the mitodl GitHub org.

Every field the provider manages is declared at its CURRENT LIVE VALUE, deliberately
including the ones that are wrong. An unset input does not mean "leave it alone" --
the provider sends its own default, so omitting a field is an unreviewed change
wearing the costume of an omission. Recording reality is what lets the empty-diff
gate mean something (plan section 6).

Values that are wrong today are marked SEC-10 below and change as a reviewed diff
against this file, not on the way in. SEC-05 is the exception: it looked like the
same shape (a wrong value pending a phase-5 flip) but turned out to be a deliberate
decision instead -- see the comment at
`dependabot_security_updates_enabled_for_new_repositories`.
"""

import pulumi_github as github
from pulumi import ResourceOptions

# Read off `GET /orgs/mitodl` on 2026-08-05. Re-check with:
#   uv run bin/github-org-inventory report --refresh
ORGANIZATION_SETTINGS = github.OrganizationSettings(
    "mitodl-organization-settings",
    billing_email="ol-engineering-finance@mit.edu",
    name="MIT Office of Digital Learning",
    description="",
    blog="https://openlearning.mit.edu/",
    location="United States",
    # --- Baseline repository access -------------------------------------------------
    # Correct as-is: members get nothing by default; access comes from teams (4.7).
    default_repository_permission="none",
    # --- Security defaults applied to NEWLY CREATED repos ---------------------------
    # `advanced_security`, `dependabot_alerts`, `dependency_graph` being false is why a
    # new repo starts without code scanning. Turning them on is the cheapest half of
    # section 3.5 tier 1: a one-time change here that every future repo inherits, with
    # no per-repo work. Left false so the import lands clean; flipping them is a
    # reviewed phase-5 diff.
    advanced_security_enabled_for_new_repositories=False,
    dependabot_alerts_enabled_for_new_repositories=False,
    # NOT part of the phase-5 flip above, and not scheduled to become one. Closed
    # 2026-08-14 as SEC-05 won't-fix: the org's shared Renovate config
    # (`mitodl/.github:renovate-config`) already sets `vulnerabilityAlerts.enabled:
    # true` plus `osvVulnerabilityAlerts: true`, so GitHub's native auto-fix PRs would
    # duplicate Renovate's PR on every repo extending it -- new repos included. See the
    # `dependabot_security_updates` comment in repositories/data/archetypes.yaml for
    # the same decision applied per-repo.
    dependabot_security_updates_enabled_for_new_repositories=False,
    dependency_graph_enabled_for_new_repositories=False,
    # Already correct. Worth noting precisely: the estate report's "org defaults
    # disable scanning for new repos" finding covers the four above, NOT these two.
    secret_scanning_enabled_for_new_repositories=True,
    secret_scanning_push_protection_enabled_for_new_repositories=True,
    # --- What members may create ----------------------------------------------------
    # SEC-10: any of 39 members can create a public repo, AND flip an existing private
    # repo public with no review step. The decision to close the second half is made
    # (2026-08-14: restrict visibility changes, leave creation alone -- see below), but
    # the SETTING ITSELF IS STILL LIVE AND UNCHANGED. There is no Pulumi resource for
    # it here or anywhere else: `members_can_change_repo_visibility` does not exist in
    # `pulumi_github.OrganizationSettings`'s 6.14.1 argument list at all -- verified
    # against the installed provider's own generated bindings, not docs. The GitHub
    # REST API has this field (`PATCH /orgs/{org}`); the Terraform/Pulumi provider
    # never wired it up. Same shape as the `Membership` exclusion in plan §4.7: not
    # modelling a setting is not the same as having already applied the decision.
    # PENDING: an org owner must still toggle it off by hand in
    # Settings -> Member privileges -> "Allow members to change repository
    # visibilities for this organization". Tracked in tk-sec-10 until that happens;
    # this comment records the decision, not its completion. Re-check for provider
    # support on any pulumi_github version bump.
    #
    # `members_can_create_public_repositories` stays true on purpose (decision
    # 2026-08-14): restricting repo/public-repo *creation* is the bigger workflow
    # change (every new OSS repo would need an owner in the loop) and was not the
    # part of SEC-10 that was actionable now.
    members_can_create_public_repositories=True,
    members_can_create_private_repositories=True,
    members_can_create_internal_repositories=False,
    members_can_create_repositories=True,
    members_can_fork_private_repositories=True,
    members_can_create_pages=True,
    members_can_create_public_pages=True,
    members_can_create_private_pages=True,
    # --- Projects -------------------------------------------------------------------
    has_organization_projects=True,
    has_repository_projects=True,
    # --- Misc -----------------------------------------------------------------------
    web_commit_signoff_required=False,
    opts=ResourceOptions(protect=True),
)
