"""Organization-wide settings for the mitodl GitHub org.

Every field the provider manages is declared at its CURRENT LIVE VALUE, deliberately
including the ones that are wrong. An unset input does not mean "leave it alone" --
the provider sends its own default, so omitting a field is an unreviewed change
wearing the costume of an omission. Recording reality is what lets the empty-diff
gate mean something (plan section 6).

Values that are wrong today are marked SEC-05 / SEC-10 below. They change in phase 5,
as a reviewed diff against this file, not on the way in.
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
    # SEC-05. These four being false is why a new repo starts without Dependabot or
    # code scanning. Turning them on is the cheapest half of section 3.5 tier 1: a
    # one-time change here that every future repo inherits, with no per-repo work.
    # Left false so the import lands clean; flipping them is a reviewed phase-5 diff.
    advanced_security_enabled_for_new_repositories=False,
    dependabot_alerts_enabled_for_new_repositories=False,
    dependabot_security_updates_enabled_for_new_repositories=False,
    dependency_graph_enabled_for_new_repositories=False,
    # Already correct. Worth noting precisely: the estate report's "org defaults
    # disable scanning for new repos" finding covers the four above, NOT these two.
    secret_scanning_enabled_for_new_repositories=True,
    secret_scanning_push_protection_enabled_for_new_repositories=True,
    # --- What members may create ----------------------------------------------------
    # SEC-10: any of 39 members can create a public repo, AND flip an existing private
    # repo public with no review step. The second half is closed 2026-08-14, but not
    # here: `members_can_change_repo_visibility` does not exist anywhere in
    # `pulumi_github.OrganizationSettings`'s 6.14.1 argument list -- verified against
    # the installed provider's own generated bindings, not docs. The GitHub REST API
    # has this field (`PATCH /orgs/{org}`); the Terraform/Pulumi provider never wired
    # it up. Same shape as the `Membership` exclusion in plan §4.7: not modelling a
    # setting is not the same as leaving it unmanaged by choice, so it is toggled off
    # by an org owner directly in Settings -> Member privileges, and this comment is
    # the only place that decision is recorded. Re-check on any provider bump.
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
