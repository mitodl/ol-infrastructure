"""The mitodl repository fleet: 316 repos as data plus a loop.

Phase 3 of docs/plans/github-org-pulumi-import.md. Every repo is described by a
YAML file in data/repos/ that declares only what it deviates from its archetype
on, so `git diff` over that directory shows exactly the drift and nothing else
(section 3.2).

BATCHING. Section 5.3 calls for importing in waves of ~25 with the empty-diff gate
after each, so that a failure in wave 4 is 25 repos to reason about rather than
316. Set the `batch` config to a comma-separated list of repo names to restrict
the stack to those:

    pulumi config set batch mit-learn,ol-django,ocw-studio
    pulumi preview

Leave it unset once the whole fleet is imported and the gate is green. The
setting exists for the import, not for steady state -- a permanently-filtered
stack would silently stop managing whatever fell outside the filter, which is the
same failure mode as a repo missing from the fleet entirely.
"""

import pulumi

from ol_infrastructure.lib.github_helper import setup_github_provider
from ol_infrastructure.saas.github.repositories import archetypes, repository, rulesets

# Must run before any github.* resource is constructed: the stack transformation
# attaches the App-authenticated provider to every one of them.
setup_github_provider()

fleet = archetypes.load_fleet()

batch = pulumi.Config().get("batch")
if batch:
    wanted = {name.strip() for name in batch.split(",") if name.strip()}
    unknown = sorted(wanted - {repo["name"] for repo in fleet})
    if unknown:
        message = f"batch names not present in the fleet: {unknown}"
        raise ValueError(message)
    fleet = [repo for repo in fleet if repo["name"] in wanted]
    pulumi.log.info(f"batch filter active: {len(fleet)} of the fleet")

for repo in fleet:
    rulesets.build(repo, repository.build(repo))
