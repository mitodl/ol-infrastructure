"""Org rulesets, targeted by the `tier` custom property.

Two rulesets replace what would otherwise be ~176 near-identical per-repo
`RepositoryRuleset` resources. Tightening the baseline becomes a one-line change to
one object rather than a fleet-wide rollout, and a new repo is protected the moment
its `tier` is set -- which, because `tier` has a default, is at creation (§3.5).

BOTH LAND AT `enforcement: evaluate`. Evaluate mode logs what *would* have been
blocked without blocking it, so the baseline can be watched against real traffic
before it starts failing anyone's push. Probe check C7 confirmed evaluate is
available on the Team plan; without it this would have to roll out blind.

  Promotion to `active` is a deliberate, separate change: flip `_ENFORCEMENT` to
  "active" after reading the rule-suite logs at
  https://github.com/organizations/mitodl/settings/rules -- and expect it to be the
  moment CI stops being advisory (SEC-03 fires on 175 of 176 active repos today).

ORDERING. These must not be applied before `ol-saas-github-repositories` has set
per-repo tiers. Until then every repo carries the property default `standard`, which
`baseline-default-branch` targets -- so applying early would sweep in all 102 forks
and 140 archived repos. Evaluate mode makes that survivable rather than harmless:
nothing would be blocked, but the rule-suite logs would be full of noise from repos
that are meant to be untargeted. See organization/custom_properties.py.

A READ-API GOTCHA THAT AFFECTS DRIFT DETECTION. Probe controls C6a/C6b established
that no read endpoint reports a non-`active` ruleset as applying to a repo: a ruleset
in evaluate mode is invisible to both `/repos/{repo}/rulesets?includes_parents=true`
and `/repos/{repo}/rules/branches/{branch}`. While these sit at `evaluate`, the only
way to see them is `/orgs/{org}/rulesets`. Any drift check that uses the
effective-rules endpoints will report them as absent and be wrong.
"""

import pulumi_github as github
from pulumi import ResourceOptions

from ol_infrastructure.saas.github.tiers import (
    TIER_ONE,
    TIER_PROPERTY_NAME,
    TIER_STANDARD,
)

#: Flip to "active" only after watching the rule-suite logs. See the module docstring.
_ENFORCEMENT = "evaluate"

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
