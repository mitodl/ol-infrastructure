"""Org rulesets, targeted by the `tier` custom property.

Two rulesets replace what would otherwise be ~176 near-identical per-repo
`RepositoryRuleset` resources. Tightening the baseline becomes a one-line change to
one object rather than a fleet-wide rollout, and a new repo is protected the moment
its `tier` is set -- which, because `tier` has a default, is at creation (§3.5).

BOTH LANDED AT `enforcement: evaluate` on 2026-08-07 and were PROMOTED TO `active` on
2026-08-14. That gap was meant to be a dry-run: evaluate mode logs what *would* have
been blocked without blocking it, watched against real traffic before anything starts
failing anyone's push.

  CORRECTION 2026-08-14 -- evaluate mode does not exist on the Team plan. The org
  settings UI states plainly: "Evaluate mode is only available to Enterprise
  organizations." Probe check C7 (`bin/github-ruleset-capability-probe`) recorded PASS
  because the API accepted `PUT enforcement=evaluate` without erroring -- it never
  confirmed GitHub actually ran dry-run logic afterward, which is exactly the kind of
  false positive the probe's own methodological note already warns about for a
  different check. In reality both rulesets sat completely inert for the week they
  spent at `evaluate`: not logging, not blocking, doing nothing. This also explains why
  no rule-suite log, at any endpoint, ever showed either ruleset evaluating a real push
  (see the read-API paragraph below -- it undersold the problem; the real issue wasn't
  read-endpoint visibility, it was that there was nothing to see).

  Consequently there was no safe way to dry-run this on the Team plan, and promotion to
  `active` on 2026-08-14 happened without one -- a deliberate decision (Tobias), not a
  gap that got missed. The two concrete risks the investigation *could* still find by
  reading existing branch-protection config directly -- renovate[bot] and
  admin/odlbot bypass -- were fixed first (see `_ADMIN_BYPASS` below and PR #5412).
  `tier-1-hardening`'s two rules (`require_last_push_approval`,
  `required_review_thread_resolution`) are brand-new requirements no repo enforced
  before; promoting straight to `active` is the first real signal on those, not a
  confirmation of already-observed safety.

  ONE KNOWN REGRESSION, ACCEPTED RATHER THAN FIXED. PR #5412 found that
  `ChristopherChudzicki` holds a personal PR-review bypass on `smoot-design` (a
  `tier-1` repo) with no equivalent under `_ADMIN_BYPASS` -- they are in
  `odl-engineering`, not `odl-engineering-owners`. `OrganizationRulesetBypassActorArgs`
  has no per-user actor type, so covering them would mean adding them to a
  bypass-eligible team. Tobias's decision (2026-08-14): let that personal exemption
  lapse rather than widen team membership to preserve it. This is a real, intentional
  behaviour change on that one repo, not an oversight.

ORDERING. These must not be applied before `ol-saas-github-repositories` has set
per-repo tiers. Until then every repo carries the property default `standard`, which
`baseline-default-branch` targets -- so applying early would sweep in all 102 forks
and 140 archived repos. See organization/custom_properties.py.

A READ-API GOTCHA THAT AFFECTS DRIFT DETECTION. Probe controls C6a/C6b established
that no read endpoint reports a non-`active` ruleset as applying to a repo: a
ruleset not at `active` enforcement is invisible to both
`/repos/{repo}/rulesets?includes_parents=true` and
`/repos/{repo}/rules/branches/{branch}`. Now that both rulesets are `active`, this
no longer matters for them, but it stays true for any future ruleset introduced at
a non-`active` enforcement -- which, per the correction above, cannot mean
`evaluate` on this plan; the only non-`active` option worth using is `disabled`,
and a disabled ruleset is invisible everywhere except `/orgs/{org}/rulesets`.
"""

import pulumi_github as github
from pulumi import ResourceOptions

from ol_infrastructure.saas.github.organization.teams import github_teams
from ol_infrastructure.saas.github.tiers import (
    TIER_ONE,
    TIER_PROPERTY_NAME,
    TIER_STANDARD,
)

#: Promoted 2026-08-14. See the module docstring for why there was no dry-run first.
_ENFORCEMENT = "active"

# Found 2026-08-14, ahead of promotion: neither ruleset carries a bypass, but
# `enforce_admins: false` on every repo's classic branch protection today means repo
# admins can already override it. An org ruleset with no bypass_actors is *more*
# restrictive than that status quo -- promoting without one would newly block
# `odlbot`, which several tier-1 repos restriction-list or bypass-list directly for
# default-branch pushes (open-edx-plugins, ocw-hugo-themes, mit-learn,
# ocw-hugo-projects). odlbot is a member of `odl-engineering-owners` (the sanctioned
# admin team, SEC-15), so bypassing at the team level restores admin parity without
# a per-user actor type, which OrganizationRulesetBypassActorArgs does not support
# (only RepositoryRole, Team, Integration, OrganizationAdmin).
#
# renovate[bot] deliberately gets NO bypass_actor entry here. It already satisfies
# `required_approving_review_count` for real: the `renovate-approve` GitHub App is
# installed org-wide and leaves a genuine APPROVED review on every renovate PR
# (confirmed live on ol-infrastructure#5343) -- a bypass would be redundant with an
# approval that already exists.
#
# arbisoft-contractors gets a `pull_request`-mode bypass (not `always`) so that they
# can merge PRs without being blocked by `require_last_push_approval`. The `always`
# mode would also let them bypass non_fast_forward and deletion on direct pushes,
# which is not intended. The `pull_request` mode only applies at PR merge time.
_ADMIN_BYPASS = [
    github.OrganizationRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=github_teams["odl-engineering-owners"].id.apply(int),
        bypass_mode="always",
    ),
    github.OrganizationRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=github_teams["arbisoft-contractors"].id.apply(int),
        bypass_mode="pull_request",
    ),
]

#: `~DEFAULT_BRANCH` is GitHub's alias for whatever each repo's default branch is,
#: which is what makes one ruleset work across a fleet where 102 repos are on
#: `master` and the rest on `main` (§3.4).
_DEFAULT_BRANCH_ONLY = github.OrganizationRulesetConditionsRefNameArgs(
    includes=["~DEFAULT_BRANCH"],
    excludes=[],
)


def _tier_condition(*tiers: str) -> github.OrganizationRulesetConditionsArgs:
    """Match repos whose `tier` property is one of `tiers`.

    `source: "custom"` distinguishes our property from GitHub's system properties
    (`visibility`, `language`, `fork`). Probe check C6c confirmed this genuinely
    matches -- the labelled repo saw the ruleset, the unlabelled control did not,
    and both read endpoints agreed -- which is the empirical basis for the whole
    org-ruleset design rather than per-repo resources.
    """
    return github.OrganizationRulesetConditionsArgs(
        ref_name=_DEFAULT_BRANCH_ONLY,
        repository_property=github.OrganizationRulesetConditionsRepositoryPropertyArgs(
            includes=[
                github.OrganizationRulesetConditionsRepositoryPropertyIncludeArgs(
                    name=TIER_PROPERTY_NAME,
                    property_values=list(tiers),
                    source="custom",
                )
            ],
            excludes=[],
        ),
    )


# Everything that is not a fork and not archived. `standard` is in scope because it
# is the property default, so a brand-new repo is covered before anyone classifies
# it -- that is the entire point of the default (§3.5).
baseline_default_branch = github.OrganizationRuleset(
    "mitodl-ruleset-baseline-default-branch",
    name="baseline-default-branch",
    target="branch",
    enforcement=_ENFORCEMENT,
    bypass_actors=_ADMIN_BYPASS,
    conditions=_tier_condition(TIER_ONE, TIER_STANDARD),
    rules=github.OrganizationRulesetRulesArgs(
        # No force-pushing over the default branch, and no deleting it.
        non_fast_forward=True,
        deletion=True,
        pull_request=github.OrganizationRulesetRulesPullRequestArgs(
            required_approving_review_count=1,
            dismiss_stale_reviews_on_push=True,
        ),
    ),
    opts=ResourceOptions(protect=True),
)

# Tier-1 is application, library and infrastructure -- 74 repos. The two extra rules
# are the ones that cost a reviewer nothing but close real gaps: an approval that
# predates the last push is not an approval of what merges, and an unresolved
# conversation is feedback that was never answered.
tier_one_hardening = github.OrganizationRuleset(
    "mitodl-ruleset-tier-1-hardening",
    name="tier-1-hardening",
    target="branch",
    enforcement=_ENFORCEMENT,
    bypass_actors=_ADMIN_BYPASS,
    conditions=_tier_condition(TIER_ONE),
    rules=github.OrganizationRulesetRulesArgs(
        pull_request=github.OrganizationRulesetRulesPullRequestArgs(
            require_last_push_approval=True,
            required_review_thread_resolution=True,
        ),
    ),
    opts=ResourceOptions(protect=True),
)

# NOT DEFINED, deliberately:
#
#   archived-freeze   Dropped 2026-08-05. It would have enforced read-only on repos
#                     GitHub already refuses writes to -- a resource to maintain for
#                     nothing. Archived repos take `tier: unmanaged` instead, which
#                     nothing targets (§5.4).
#
#   required_status_checks
#                     Stays per-repo. Check names differ (`javascript-tests` vs
#                     `python-tests`), so there is no fleet-wide value to assert.
#                     This is why DX-02 and DX-03 remain per-repo audit rules.
#
#   copilot_code_review
#                     Supported on OrganizationRuleset and arguably belongs here --
#                     six repos carry a hand-made per-repo ruleset for it today. Left
#                     out pending the decision in the Copilot governance task (§4.6).
