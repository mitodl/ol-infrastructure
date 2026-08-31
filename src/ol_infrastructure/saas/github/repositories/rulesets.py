"""Per-repo rulesets: required status checks, and nothing else.

SEC-03 -- every repo in the org runs CI and none of it can block a merge. Closing that
needs the one rule the §5.4 org-ruleset design deliberately left per-repo, because check
names are per-repo facts and no org-wide ruleset can name them all.

THERE IS NO DRY RUN. `enforcement: evaluate` does not exist on the Team plan -- the API
accepts the PUT and then does nothing at all (see organization/org_rulesets.py, which
spent a week inert believing otherwise). So a ruleset built here goes straight to
`active`, and a `context` string that does not exactly match a real check-run name
permanently blocks every PR on that repo with no signal other than a stuck PR. That is
the DX-02 failure mode, caused by the fix for SEC-03.

The only defence is that the names are checked against reality before they land:

    uv run bin/github-required-checks sample mit-learn --prs 20

scores each check name against the merged PRs where it COULD have reported -- the ones
whose head commit defines the job and whose workflow actually ran -- and only a name it
marks SAFE belongs in `required_status_checks`. A name missing from a PR that defined it
and ran its workflow is not a flake to round up: it is a check that some PR did not
produce, and requiring it blocks that kind of PR forever.

That scope is what makes the aggregate gates below requirable at all. A raw
"appeared on N of the last 40" reads a job added last week (`ci-gate`, 31/40 on
ol-infrastructure, every miss a branch cut before it existed) identically to a
path-filtered one (`Run zizmor`, 1/40, and permanently unrequirable). Only the first is
safe, and no percentage separates them.

WHAT MAKES A NAME UNSAFE TO REQUIRE, all observed live in this org on 2026-08-21:

  path-filtered workflows   `ol-infrastructure`'s `Run zizmor` only runs on PRs
                            touching `.github/workflows/**` (1 of the last 40). A
                            workflow that does not trigger produces no check run at
                            all, so the ruleset waits forever rather than passing.
                            The `on:` block states this outright, so
                            `bin/github-required-checks` reads it there and refuses
                            the name rather than inferring it from a count.
  unreliable reviewers      `Seer Code Review` (22/40) and
                            `copilot-pull-request-reviewer` (11/40) attach only
                            sometimes. They are advisory by design.
  matrix shard names        `mitxonline`'s `python-tests (1)`..`(4)`. See below: this is
                            not a hypothetical, the rename already happened.
  checks that fail on       A check that reports reliably can still fail on purpose.
  purpose                   `openapi-diff` is the case, and requiring it needed an
                            allowlist in the repo that produces it before it was
                            reasonable -- see the note on mit-learn below. Reporting
                            reliably and passing reliably are different properties;
                            only the first is a sampling question.

SAMPLE SIZE IS PART OF THE ANSWER. On 2026-08-21 `CodeQL` and `Analyze (*)` looked
perfectly stable at 20 PRs on ol-infrastructure and dropped to 38/40 at forty. (They
read 40/40 again on 2026-08-28, which is the point rather than a correction: a name
can look clean at any single window.) Twenty is not enough to clear a name.

THE MATRIX RENAME IS NOT A THOUGHT EXPERIMENT. In February 2026 mitxonline produced one
check named `python-tests`. Commit 63ded115 on 2026-07-23 ("Shard CI pytest run across a
4-way matrix") replaced it with `python-tests (1)`..`(4)`. Had this file required
`python-tests` before that date: the sharding PR blocks itself, somebody bypasses it
because the tests plainly pass, and from then on EVERY mitxonline PR blocks forever --
cause in one repo, symptom in a second, fix in a third. mitxonline also already
carries the shard count twice in its own ci.yml (`matrix: group: [1, 2, 3, 4]` and
`--splits 4`); a third copy over here is the wrong direction. Hence: no shard names
are required today, and `_allow_matrix_shard_checks` exists so that changing that
has to be deliberate.

WHAT IS ACTUALLY REQUIRED IS AN AGGREGATE GATE JOB, one per repo, and not the list of
job names an earlier draft of this file carried. A `ci-gate` job with `needs:` every
other job in its workflow, required as a single static context, puts the list of what
must pass in the file that owns the jobs -- so resizing a matrix and updating the gate
are one edit by one person in one repo, and the failure modes above stop being reachable
from here at all. `lehrer` had already done this for itself (`gate`, `fast-checks`)
before this work started.

Those jobs have since landed: ol-infrastructure #5567 (2026-08-24), mitxonline #3876
(2026-08-25), mit-learn #3825. Each `needs:` every job in its workflow and runs under
`if: always()`, without which a failed dependency skips the gate and GitHub reads a
skipped required check as satisfied.

`mit-learn` requires `openapi-diff` as well, by its own job name, and that is not the
list creeping back. `needs:` only reaches jobs in the same workflow file: mit-learn's
oasdiff check lives in `openapi-diff.yml` on a `pull_request` trigger while `ci-gate`
sits in `ci.yml` on `push`, so no gate can absorb it.

Giving that workflow a gate of its own was tried and reverted (mit-learn #3856). It buys
only protection against somebody renaming a single job, since the workflow has one job
and no matrix -- and it pays for that with a required context carrying no history, an
extra job on every PR, and a second repo that has to merge before this stack can be
applied. `openapi-diff` has reported on 40 of the last 40 mit-learn PRs. A rename would
be caught by `drift` as a loud CI failure rather than a wedged repo, which is the risk
the gate was supposed to cover.

Requiring it at all is safe only because mit-learn #3825 gave it an `--err-ignore`
allowlist (`openapi/oasdiff-err-ignore.txt`): before that it went red on INTENTIONAL
breaking API changes -- red at merge on 8 of the last 40 mit-learn PRs, release PRs
included -- and requiring it would have forced a bypass on every deliberate API change,
which trains people to bypass. Now such a change adds a line to a reviewed file in the
repo that made it. If that allowlist turns out to be unusable in practice, drop
`openapi-diff` from mit-learn's YAML; `ci-gate` is unaffected.

WHAT THE GATE DOES NOT FIX is a branch older than the gate itself. A PR cut before
`ci-gate` existed does not define the job, so it produces no such check and hangs with
nothing to re-run -- and unlike a wrong name this resolves itself on a rebase. On
2026-08-28 that was 45 of 46 open mit-learn PRs, 37 of 45 on mitxonline and 19 of 44 on
ol-infrastructure. `bin/github-required-checks blocked` lists them by name and exits
non-zero, and is the check to run immediately before `pulumi up` rather than after.

BYPASS MIRRORS THE ORG RULESETS, AND THAT IS A NARROWER CHOICE THAN IT LOOKS.
`_BYPASS` copies `_ADMIN_BYPASS` from organization/org_rulesets.py: owners `always`,
`odl-engineering` and `arbisoft-contractors` at `pull_request`. So a human on either
team can still merge a red build, and for humans this rule is advisory.

That is deliberate, and it still fixes the failure that prompted the work, because
bypass actors are TEAMS and the actor that caused the harm is an App. On 2026-08-21
Renovate merged mit-learn #3812-#3816 in 71 seconds, two of them with `python-tests`
already red on their own branch, breaking `main` and costing a revert (#3822) three
hours later. The same thing had been running quietly on ol-infrastructure: 12 of the
last 40 merges landed with `test` red, and every single one was a Renovate PR
(#5529-#5537, #5553-#5555). `renovate[bot]` is in no team, so no bypass here applies to
it, and GitHub's auto-merge honours required checks regardless of who is bypass-listed.

The chain that produced those merges: the shared Renovate config extends
`:automergeMinor`, `platformAutomerge` defaults to `true`, so Renovate hands the merge
to GitHub auto-merge, which waits for "all required reviews and status checks". Required
checks were zero, so the wait was instant and the approval from the `renovate-approve`
App was the only gate. Auto-merge is not the bug; the empty list was.

TEAM IDS COME FROM `archetypes.TEAM_IDS`, not from the `organization` project's
`teams.py`. They are two separate Pulumi projects; importing across them would
re-declare every `Team` resource inside this stack. Same reasoning as
`repository.py`'s `TeamRepository`.
"""

import re
from typing import Any

import pulumi_github as github
from pulumi import ResourceOptions

from ol_infrastructure.saas.github.repositories import archetypes

#: Mirrors `_ADMIN_BYPASS` in organization/org_rulesets.py exactly (decision: Tobias,
#: 2026-08-21). See the module docstring for what that does and does not buy.
_BYPASS = [
    github.RepositoryRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=archetypes.TEAM_IDS["odl-engineering-owners"],
        bypass_mode="always",
    ),
    github.RepositoryRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=archetypes.TEAM_IDS["arbisoft-contractors"],
        bypass_mode="pull_request",
    ),
    github.RepositoryRulesetBypassActorArgs(
        actor_type="Team",
        actor_id=archetypes.TEAM_IDS["odl-engineering"],
        bypass_mode="pull_request",
    ),
]

#: GitHub's alias for whatever the repo's default branch is, so this does not have to
#: know which of `main` or `master` a repo is on. Same device as the org rulesets.
_DEFAULT_BRANCH_ONLY = github.RepositoryRulesetConditionsArgs(
    ref_name=github.RepositoryRulesetConditionsRefNameArgs(
        includes=["~DEFAULT_BRANCH"],
        excludes=[],
    ),
)

#: The name GitHub shows for the ruleset this module creates. Shared with
#: `bin/github-org-inventory`, which uses it to tell the checks WE require from the
#: ones a repo required for itself before Pulumi got there -- the two round-trip into
#: different YAML keys. A literal in both places would drift.
REQUIRED_CHECKS_RULESET_NAME = "required-status-checks"

#: A check name ending in `(...)`: GitHub's rendering of one cell of a job matrix.
#: Matching it is not a correctness check -- these names are perfectly valid contexts
#: and requiring them works today. It exists so that requiring one is a decision
#: somebody made in the repo's YAML rather than a name that got copied out of a sample.
_MATRIX_SHARD = re.compile(r"\(.+\)$")


def _check(
    context: str,
) -> github.RepositoryRulesetRulesRequiredStatusChecksRequiredCheckArgs:
    """One required context.

    `integration_id` is left unset. Pinning a context to the app that produces it would
    stop a different app satisfying `test` with a green check of its own, but the ids
    are per-app facts the crawl does not record, and an id that is merely *wrong*
    produces the same permanent block a wrong name does. Worth adding once the crawl
    records them; not worth guessing.
    """
    return github.RepositoryRulesetRulesRequiredStatusChecksRequiredCheckArgs(
        context=context,
    )


#: The name GitHub shows for the release-branch ruleset this module creates.
RELEASE_BRANCHES_RULESET_NAME = "protected-release-branches"


def _release_branch_conditions(
    branches: list[str],
) -> github.RepositoryRulesetConditionsArgs:
    return github.RepositoryRulesetConditionsArgs(
        ref_name=github.RepositoryRulesetConditionsRefNameArgs(
            includes=[f"refs/heads/{branch}" for branch in branches],
            excludes=[],
        ),
    )


def build_release_branches(repo: dict[str, Any], repository: github.Repository) -> None:
    """Protect a repo's release branches from deletion and force-push.

    Targets `release`/`release-candidate`-style branches on repos still driven by
    the release-script bot ("Doof"): a persistent `release-candidate` branch gets
    promoted to `release` by merging a PR whose head is `release-candidate`, and the
    fleet-wide `delete_branch_on_merge` default (converged in #5468) deletes a PR's
    head branch on merge -- so without an explicit rule here, promoting a release
    deletes the branch the next release cycle needs to exist.

    This happened to `micromasters`: its `release-candidate` branch was deleted and
    never recreated, and its `release` branch carried no protection at all (2026-08-31
    audit). It is exactly the class of "invisible drift" org_rulesets.py warns
    about -- classic per-branch protection is not visible to this stack and isn't
    swept along with the rest of the org-ruleset migration, so restoring it here
    (rather than by hand through the UI) is what keeps a future sweep from silently
    dropping it again.

    Deliberately narrower than `build()`'s default-branch ruleset: only `deletion`
    and `non_fast_forward` are required. Nothing here asserts review counts or
    status checks on these branches -- they receive fast-forward promotions from a
    bot, not reviewed PRs from contributors, and `baseline-default-branch` /
    `tier-1-hardening` already don't apply here since both scope to
    `~DEFAULT_BRANCH` only.
    """
    branches = repo.get("protected_release_branches")
    if not branches:
        return

    name = repo["name"]
    github.RepositoryRuleset(
        f"mitodl-repo-protected-release-branches-{name}",
        name=RELEASE_BRANCHES_RULESET_NAME,
        repository=name,
        target="branch",
        enforcement="active",
        bypass_actors=_BYPASS,
        conditions=_release_branch_conditions(sorted(branches)),
        rules=github.RepositoryRulesetRulesArgs(
            non_fast_forward=True,
            deletion=True,
        ),
        opts=ResourceOptions(protect=True, depends_on=[repository]),
    )


def build(repo: dict[str, Any], repository: github.Repository) -> None:
    """Emit the required-status-checks ruleset for one repo, if it declares any.

    Absent `required_status_checks` means no resource at all rather than an empty
    ruleset. An empty `required_checks` list is a ruleset that requires nothing, which
    reads in the GitHub UI as protection that exists and enforces nothing -- the same
    confusion SEC-03 is about.

    `repository` IS REQUIRED EVEN THOUGH THE RULESET TAKES A NAME STRING. `repository=`
    is a plain `str`, not an `Output`, so Pulumi infers no dependency from it and is
    free to create the ruleset before the repo exists. Every one of the three repos in
    the first wave is already in state, so the ordering is invisible today and would
    first fail on whichever new repo declared checks next. Same reasoning, and the same
    fix, as `BranchDefault` and `TeamRepository` in repository.py.
    """
    contexts = repo.get("required_status_checks")
    if not contexts:
        return

    name = repo["name"]
    github.RepositoryRuleset(
        f"mitodl-repo-required-status-checks-{name}",
        name=REQUIRED_CHECKS_RULESET_NAME,
        repository=name,
        target="branch",
        enforcement="active",
        bypass_actors=_BYPASS,
        conditions=_DEFAULT_BRANCH_ONLY,
        rules=github.RepositoryRulesetRulesArgs(
            required_status_checks=github.RepositoryRulesetRulesRequiredStatusChecksArgs(
                required_checks=[_check(context) for context in sorted(contexts)],
                # "Require branches to be up to date before merging". Left OFF
                # deliberately: on a repo merging several PRs an hour it serialises
                # every merge behind a re-run of the full suite, and the org's busiest
                # repos are exactly the ones this rule targets first. It buys protection
                # against semantic conflicts between concurrently-merged PRs, which is a
                # real but much rarer failure than the throughput it costs.
                strict_required_status_checks_policy=False,
            ),
        ),
        opts=ResourceOptions(protect=True, depends_on=[repository]),
    )
