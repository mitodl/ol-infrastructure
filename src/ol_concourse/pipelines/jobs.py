"""Infrastructure job builders pre-configured with project defaults.

Re-exports packer_jobs, pulumi_job, and pulumi_jobs_chain from
ol_concourse.lib.jobs.infrastructure. pulumi_jobs_chain is wrapped with
functools.partial to pre-fill github_issue_repository with the default
repository used across ol-infrastructure pipelines.
"""

from functools import partial

from ol_concourse.lib.jobs.infrastructure import (
    packer_jobs,
    pulumi_job,
)
from ol_concourse.lib.jobs.infrastructure import (
    pulumi_jobs_chain as _pulumi_jobs_chain,
)

from ol_concourse.pipelines.constants import GH_ISSUES_DEFAULT_REPOSITORY

pulumi_jobs_chain = partial(
    _pulumi_jobs_chain,
    github_issue_repository=GH_ISSUES_DEFAULT_REPOSITORY,
)

__all__ = ["packer_jobs", "pulumi_job", "pulumi_jobs_chain"]
