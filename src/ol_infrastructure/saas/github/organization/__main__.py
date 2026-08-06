"""Management of the mitodl GitHub organization, its teams, and the `tier` schema.

Phases 2 and 3.5 of docs/plans/github-org-pulumi-import.md: org settings, the 14
teams, the `tier` custom property, and the two org rulesets that target it.

Deliberately absent, each for a reason worth knowing:

  Membership / TeamMembership
      Pulumi manages teams, not people (§4.7). Repo access flows through TeamRepository
      in the repositories project, so modelling individuals would buy a declared roster
      at the cost of a PR per hire, departure and team move -- on a stack gated behind
      manual approval. It also removes the highest-blast-radius resource in the estate,
      since deleting a Membership evicts a human from the org.

  OrganizationWebhook
      None exist (crawl 2026-08-05).

  OrganizationCustomRole / OrganizationRepositoryRole
      Enterprise-only; 404 on the Team plan. Withdrawn from scope entirely.

Everything except `tier` is an import of something that already exists, so after the
first apply this stack should preview clean. That empty diff is the gate (§6).
"""

from ol_infrastructure.lib.github_helper import setup_github_provider

# Must run before any github.* resource is constructed: the stack transformation
# attaches the App-authenticated provider to every one of them.
setup_github_provider()

from ol_infrastructure.saas.github.organization import (  # noqa: E402, F401
    custom_properties,
    org_rulesets,
    org_settings,
    teams,
)
