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

  THAT SIGNAL CAME BACK NEGATIVE ON ONE OF THE TWO. `require_last_push_approval` was
  dropped on 2026-08-17 (#5459) after three days of real use, along with
  `dismiss_stale_reviews_on_push` on the baseline -- both punished the ordinary
  approve-with-a-nit review by invalidating an approval the author's cleanup push was
  meant to satisfy. `required_review_thread_resolution` stays. See each ruleset below.

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

from bridge.settings.apps import RELEASE_BOT_APP_ID
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
#
# That original motive is now moot -- `require_last_push_approval` was dropped in #5459
# -- but these entries stay. They are what keeps `required_review_thread_resolution` and
# the baseline's approval requirement overridable at merge time by the teams that own
# these repos, which is the same admin parity `enforce_admins: false` used to provide.
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
    github.OrganizationRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=github_teams["odl-engineering"].id.apply(int),
        bypass_mode="pull_request",
    ),
    # ol-release-bot, added by hand on 2026-09-01 and registered here so an apply
    # does not revert it. The Concourse `release` resource's `action: finish` merges
    # `releases/<version>` into the default branch and pushes it directly, which
    # `baseline-default-branch`'s `pull_request` rule rejected outright:
    #
    #   remote: error: GH013: Repository rule violations found for refs/heads/main.
    #   remote: - Changes must be made through a pull request.
    #
    # That failure stranded ol-analytics-api's `releases/2026.8.28.2` on the remote,
    # deployed to production but never merged back, until it was finished by hand
    # (mitodl/ol-analytics-api#43).
    #
    # BLAST RADIUS, STATED PLAINLY: this ADDS a bypass actor and removes none. The App
    # can now push directly to, force-push, and delete the default branch of every
    # `tier-1`/`standard` repo it is installed on, without review. Two things bound
    # that, and neither is enforced here: the installation is `selected` rather than
    # org-wide, and the App holds only `contents`/`deployments`/`issues` write.
    #
    # It is not a net privilege reduction, and an earlier draft of this comment was
    # wrong to call it one. The capability is one the release workflow ALREADY
    # exercises -- the legacy release-script bot ("Doof") does the identical direct
    # push and was never blocked, because its `odlbot` identity is a member of
    # `odl-engineering-owners` above and inherits that team's `always` bypass; it
    # pushed mit-learn 31d67335 ("Release date for 0.78.2", no associated PR) straight
    # onto that repo's default branch on 2026-09-01, three weeks after these rulesets
    # went `active`. So the fleet gains no capability it lacked. It gains a second
    # holder of that capability, and only becomes a swap when Doof is decommissioned
    # and odlbot leaves the owners team (mitodl/hq#7185).
    #
    # `always` rather than `pull_request`, unlike the two contractor teams: the whole
    # point is a direct push, and a `pull_request`-mode bypass only relaxes rules at
    # PR merge time. Opening a PR instead was considered and does not work -- the
    # App's installation holds `pull_requests: read`, so it can neither open nor merge
    # one, and `required_approving_review_count: 1` above would still want a human.
    github.OrganizationRulesetBypassActorArgs(
        actor_type="Integration",
        actor_id=RELEASE_BOT_APP_ID,
        bypass_mode="always",
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
        # `dismiss_stale_reviews_on_push` dropped in #5459: it cost contributors more
        # than it protected. Any push revoked an approval that was green a moment
        # earlier -- a rebase, a lint fix, a typo in a comment -- so PRs bounced back to
        # "review required" for changes nobody needed to re-read. What remains is the
        # bare requirement that SOME approval exists; see `tier_one_hardening` below,
        # which no longer backstops it.
        #
        # BOTH REVIEW FLAGS HERE MUST BE WRITTEN AS EXPLICIT `False`. The provider
        # treats them as optional-and-computed, so deleting the line is not the same as
        # turning the rule off: the preview comes back clean, the code reads as though
        # the rule were gone, and GitHub goes on enforcing it. Anything that looks like
        # a redundant `False` in this block is load-bearing.
        pull_request=github.OrganizationRulesetRulesPullRequestArgs(
            required_approving_review_count=1,
            dismiss_stale_reviews_on_push=False,
        ),
    ),
    opts=ResourceOptions(protect=True),
)

# Tier-1 is application, library and infrastructure -- 74 repos. Down to ONE extra rule:
# an unresolved conversation is feedback that was never answered, and blocking on it
# costs a reviewer nothing.
#
# `require_last_push_approval` dropped in #5459, having been added on 2026-08-07 with
# the reasoning that "an approval that predates the last push is not an approval of what
# merges". That is true in the abstract and wrong in the common case. The routine review
# is an approval WITH a minor cleanup request -- fix the typo, rename the variable,
# then merge. Under this rule the author's cleanup push invalidated the approval that
# explicitly anticipated it, and shipping a one-line change then required the reviewer
# to come back for a second round that told them nothing new. It deadlocked two people
# over work already agreed to.
#
# What is genuinely lost: an author can now push anything after approval and merge it
# unreviewed. `required_review_thread_resolution` does not cover that -- it gates on
# threads being answered, not on the diff being re-read. This is accepted, not
# overlooked: the same push is equally unreviewed under a rule everyone routes around,
# and `baseline-default-branch` still requires an approval to exist at all.
#
# The `False` is required, not redundant -- see the note in `baseline_default_branch`.
tier_one_hardening = github.OrganizationRuleset(
    "mitodl-ruleset-tier-1-hardening",
    name="tier-1-hardening",
    target="branch",
    enforcement=_ENFORCEMENT,
    bypass_actors=_ADMIN_BYPASS,
    conditions=_tier_condition(TIER_ONE),
    rules=github.OrganizationRulesetRulesArgs(
        pull_request=github.OrganizationRulesetRulesPullRequestArgs(
            require_last_push_approval=False,
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
#
# THE CLASSIC BRANCH PROTECTION STILL OUT THERE IS NOT INERT. These rulesets are
# ADDITIVE to it, not a replacement: an action must satisfy classic protection AND
# every ruleset that matches, and a `bypass_actors` entry here grants no relief from the
# classic layer at all. Whatever classic protection remains is invisible drift --
# repository.py declines to declare BranchProtection -- and can override everything
# decided in this file.
#
# open-edx-plugins made that concrete on 2026-08-17. Its classic protection restricted
# pushes to `main` to the single user `odlbot`, and GitHub counts merging a pull request
# as a push -- so arbisoft-contractors and odl-engineering could not merge, and adding
# them as `pull_request` bypass actors above did nothing, because the block was never in
# a ruleset. It had been masked for months by both teams holding `admin` while
# `enforce_admins` was false; PR #5324 downgrading them to `push` on 2026-08-12 removed
# that accidental bypass and broke merges outright.
#
# Resolved by DELETING that repo's classic protection entirely (Tobias, 2026-08-17) --
# every protection it asserted was already met or exceeded here (`baseline` matches its
# review count, `tier-1-hardening` adds thread resolution, both cover force-push and
# deletion). The two rules dropped later that same day (#5459) do not reopen a gap on
# this repo specifically: its classic protection had `dismiss_stale_reviews: false` and
# `require_last_push_approval: false` already, so the rulesets are still no weaker than
# what was deleted. Only `block_creations` was lost, which is inert on a branch that
# already exists.
#
# THE FLEET WAS THEN SWEPT (2026-08-17). `mit-learn` turned out to carry the identical
# odlbot-only restriction, silently blocking three teams with nobody reporting it, so
# this was never a one-repo problem. Default-branch classic rules were deleted on 16
# repos; NO PUSH RESTRICTION SURVIVES ON ANY ACTIVE REPO -- deliberately not "anywhere",
# since the seven archived repos below were never examined -- and SEC-16 in audit.py now
# reports this class of block rather than leaving it to a contractor with a stuck PR.
#
# WHAT IS LEFT, and why each part was kept rather than missed:
#
#   4 active, default branch   `handbook` (the only `enforce_admins: true` repo) and
#                              three `frontend-*-mitol` forks. The forks are tier
#                              `unmanaged`, so NO ruleset here targets them -- their
#                              classic rule is their only protection, not a redundant
#                              one, and deleting it would have left them bare.
#
#   7 active, release branches `release*` / `release-candidate` on mit-learn,
#                              mitxonline, mitxpro, ocw-hugo-themes, ocw-studio,
#                              odl-video-service and open-discussions. They block
#                              force-push and deletion, and every ruleset here targets
#                              `~DEFAULT_BRANCH` only, so nothing replaces them.
#
#   7 archived                 bootcamp-ecommerce, ccxcon, mit-open-login-button,
#                              ol-npm-libraries, open-discussions-client,
#                              unified-ecommerce, unified-ecommerce-frontend. NOT
#                              EXAMINED: both sweeps filtered `isArchived: false`. An
#                              archived repo is read-only so a push restriction there
#                              blocks nothing today, but this is an unmeasured set, not
#                              a cleared one -- and it becomes real the moment one is
#                              unarchived.
#
# COUNT THESE FROM THE CODE, NOT FROM `_has_branch_protection`. That field records the
# DEFAULT BRANCH only, so it reads 11 -- it cannot see the seven release-branch rules at
# all. A note written from that field alone would be accurate and still wrong.
