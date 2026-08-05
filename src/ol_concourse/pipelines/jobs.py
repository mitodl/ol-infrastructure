"""Infrastructure job builders pre-configured with project defaults.

Re-exports packer_jobs, pulumi_job, and pulumi_jobs_chain from
ol_concourse.lib.jobs.infrastructure. pulumi_jobs_chain is wrapped to
pre-fill github_issue_repository with the default repository used across
ol-infrastructure pipelines while still allowing callers to override it.

Both Pulumi builders are additionally wrapped to strip the upstream
``attempts=2`` retry from the pulumi-provisioner ``put`` step.  See
``_drop_pulumi_retry`` for why.
"""

from collections.abc import Iterator

from ol_concourse.lib.jobs.infrastructure import (
    packer_jobs,
)
from ol_concourse.lib.jobs.infrastructure import (
    pulumi_job as _pulumi_job,
)
from ol_concourse.lib.jobs.infrastructure import (
    pulumi_jobs_chain as _pulumi_jobs_chain,
)
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import PutStep
from pydantic import BaseModel

from ol_concourse.pipelines.constants import GH_ISSUES_DEFAULT_REPOSITORY

PULUMI_RESOURCE_PREFIX = "pulumi-"


def _drop_pulumi_retry(fragment: PipelineFragment) -> PipelineFragment:
    """Remove the retry wrapper from the pulumi-provisioner ``put`` step.

    Upstream ``pulumi_job`` sets ``attempts=2`` on that step.  A retried Pulumi
    ``put`` can report a build as *succeeded* when the update actually failed,
    which is worse than a flaky red build: a green Pulumi job fires
    ``on_success``, which posts the ``[bot] Pulumi <project> <stack> deployed.``
    issue -- and closing that issue is what promotes the change to the next
    environment.  A fabricated success therefore hands a human a promotion gate
    to QA and on to Production for a deploy that never happened.

    Observed in ``docker-pulumi-keycloak/deploy-ol-substructure-keycloak-ci/158``
    (Concourse build 437070850, 2026-08-05), recorded as **succeeded** in 1h6m58s:

        finish-put 6a736727 exit=1     <- real failure, "2 errored, update failed"
        error      6a736728            <- 2nd attempt never ran Pulumi; the worker
                                          holding its input volume was gone
                                          ("connection refused" streaming
                                          ol-infrastructure-pulumi-substructure)
        finish-put 6a736727 exit=0     <- re-executed and reported success while
                                          emitting no Pulumi output at all: no
                                          "Refreshing", no "Updating", no
                                          "Resources:", no "Duration:"

    It then posted mitodl/ol-infrastructure#76974 announcing the deploy.  Ground
    truth disagrees: the next build still had to delete the two Keycloak roles a
    converged stack would already have removed.

    Retrying is not worth that risk here.  ``pulumi up`` is a mutating operation
    and the provisioner resource already recovers from the common transient case
    itself (stack-lock recovery), so the retry buys little.  Setting ``attempts``
    to ``None`` drops the ``retry`` wrapper from the emitted plan entirely rather
    than emitting a one-child retry.

    The real home for this is ``pulumi_job`` in mitodl/ol-concourse; this is the
    chokepoint that protects every ol-infrastructure pipeline until that lands.
    """
    for job in fragment.jobs:
        for step in _pulumi_puts(job):
            step.attempts = None
    return fragment


def _pulumi_puts(node: object) -> Iterator[PutStep]:
    """Every pulumi-provisioner ``put`` anywhere in a job, at any nesting depth.

    ``pulumi_job`` puts its Pulumi step directly in ``job.plan`` today — all 89
    of them across the rendered pipelines sit at the top level, none inside a
    composite step. So this walks deeper than it strictly has to.

    That is deliberate. A ``PutStep`` nested in an ``InParallelStep`` / ``DoStep``
    / ``TryStep``, or hung off an ``on_success``/``ensure`` hook, would be missed
    by a one-level scan and would silently keep ``attempts=2`` — which brings
    back exactly the failure this function exists to prevent, and brings it back
    invisibly. Recursing over the model tree costs nothing and does not depend on
    upstream keeping the plan flat.
    """
    if (
        isinstance(node, PutStep)
        and node.put
        and node.put.startswith(PULUMI_RESOURCE_PREFIX)
    ):
        yield node
    if isinstance(node, BaseModel):
        for field in type(node).model_fields:
            yield from _pulumi_puts(getattr(node, field, None))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _pulumi_puts(item)


def pulumi_job(*args, **kwargs) -> PipelineFragment:
    """Create a Pulumi job pre-configured for ol-infrastructure.

    Wraps ol_concourse.lib.jobs.infrastructure.pulumi_job; all parameters are
    forwarded unchanged.  The only difference is that the Pulumi ``put`` step is
    not retried -- see :func:`_drop_pulumi_retry`.
    """
    return _drop_pulumi_retry(_pulumi_job(*args, **kwargs))


def pulumi_jobs_chain(
    *args,
    github_issue_repository: str = GH_ISSUES_DEFAULT_REPOSITORY,
    **kwargs,
) -> PipelineFragment:
    """Create a chained sequence of Pulumi jobs pre-configured for ol-infrastructure.

    Wraps ol_concourse.lib.jobs.infrastructure.pulumi_jobs_chain with
    project-scoped defaults.  All parameters accepted by the upstream function
    (including ``refresh_stack``) are forwarded via ``**kwargs``.  The Pulumi
    ``put`` steps are not retried -- see :func:`_drop_pulumi_retry`.
    """
    return _drop_pulumi_retry(
        _pulumi_jobs_chain(
            *args, github_issue_repository=github_issue_repository, **kwargs
        )
    )


__all__ = ["packer_jobs", "pulumi_job", "pulumi_jobs_chain"]
