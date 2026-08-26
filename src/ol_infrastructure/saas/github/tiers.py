"""The `tier` vocabulary, shared by both GitHub Pulumi projects.

`tier` is the only value that crosses between `organization` and `repositories`.
The organization project defines the custom property and the rulesets that target
it by name; the repositories project sets each repo's value. There is no
StackReference between them -- the coupling is this shared vocabulary, which is
exactly why it lives in one module.

Importing this in both places is what makes a typo fail at plan time. A misspelled
tier written as a bare string would otherwise be accepted by GitHub, match no
ruleset, and leave the repo silently unprotected -- indistinguishable from a repo
nobody labelled.
"""

from typing import Final

#: Repos that get the full baseline plus the tier-1 hardening ruleset.
TIER_ONE: Final = "tier-1"

#: The landing pad. No archetype assigns this -- it is the custom property's DEFAULT,
#: so a repo carries it from creation until someone classifies it.
#: `baseline-default-branch` targets it precisely so a new repo is protected at
#: creation rather than whenever a PR gets around to it (plan section 3.5). A
#: non-trivial number of repos sitting here means classification has fallen behind
#: repo creation.
TIER_STANDARD: Final = "standard"

#: Deliberately untargeted: upstream forks, archived repos, and -- since 2026-08-26 --
#: one ACTIVE repo, `ol-django`, whose release flow pushes a version-bump commit
#: straight to its default branch. See the `library-unmanaged` archetype in
#: repositories/data/archetypes.yaml for why, and what it costs.
#:
#: THAT THIRD CASE IS NOT LIKE THE OTHER TWO. Forks and archived repos are untargeted
#: because no ruleset could usefully apply to them -- upstream owns the fork's branch,
#: and GitHub already refuses writes to an archived repo. An ACTIVE repo here gives up
#: real protection: no required review, and no `non_fast_forward` or `deletion` cover on
#: its default branch, because targeting is per-ruleset rather than per-rule. SEC-01
#: exempts `unmanaged`, so the audit reports none of it. Putting an active repo in this
#: tier is a security decision, not a classification.
#:
#: Distinct from "unlabelled" -- this is a claim that no org ruleset should apply, which
#: a reviewer can disagree with. Without it, CON-09 could not tell an intentional
#: exemption from an oversight.
TIER_UNMANAGED: Final = "unmanaged"

#: Order matters only for readability; GitHub stores allowed_values as a set.
TIER_VALUES: Final = (TIER_ONE, TIER_STANDARD, TIER_UNMANAGED)

#: The custom property name, referenced by rulesets and by per-repo values alike.
TIER_PROPERTY_NAME: Final = "tier"
