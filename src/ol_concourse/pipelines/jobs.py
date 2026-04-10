"""Infrastructure job builders pre-configured with project defaults.

Re-exports packer_jobs, pulumi_job, and pulumi_jobs_chain from
ol_concourse.lib.jobs.infrastructure. pulumi_jobs_chain is wrapped to
pre-fill github_issue_repository with the default repository used across
ol-infrastructure pipelines while still allowing callers to override it.
"""

from ol_concourse.lib.jobs.infrastructure import (
    packer_jobs,
    pulumi_job,
)
from ol_concourse.lib.jobs.infrastructure import (
    pulumi_jobs_chain as _pulumi_jobs_chain,
)

from ol_concourse.pipelines.constants import GH_ISSUES_DEFAULT_REPOSITORY


def pulumi_jobs_chain(
    *args,
    github_issue_repository: str = GH_ISSUES_DEFAULT_REPOSITORY,
    **kwargs,
):
    return _pulumi_jobs_chain(
        *args, github_issue_repository=github_issue_repository, **kwargs
    )


__all__ = ["packer_jobs", "pulumi_job", "pulumi_jobs_chain"]
