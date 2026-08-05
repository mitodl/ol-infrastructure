"""The Pulumi ``put`` step must never be retried.

A retried Pulumi ``put`` can report a build as succeeded when the update
failed, which fires ``on_success`` and posts the promotion-gate issue for a
deploy that never happened.  See ``_drop_pulumi_retry`` for the build that
demonstrated it.
"""

from pathlib import Path

import pytest
from ol_concourse.lib.jobs.infrastructure import pulumi_job as upstream_pulumi_job
from ol_concourse.lib.models.pipeline import Identifier, PutStep
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.jobs import pulumi_job, pulumi_jobs_chain


@pytest.fixture
def pulumi_code():
    return git_repo(
        name=Identifier("ol-infrastructure-pulumi-test"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=["src/ol_infrastructure/"],
    )


def _pulumi_put_steps(fragment):
    return [
        step
        for job in fragment.jobs
        for step in job.plan
        if isinstance(step, PutStep) and step.put and step.put.startswith("pulumi-")
    ]


def test_upstream_still_sets_the_retry(pulumi_code):
    """Guard the premise: if upstream drops attempts=2, this wrapper is redundant.

    A failure here means ol-concourse fixed it and the local override in
    ``src/ol_concourse/pipelines/jobs.py`` (``_drop_pulumi_retry``, and the two
    wrappers that call it) can go away -- it does not mean this repo regressed.
    """
    fragment = upstream_pulumi_job(
        pulumi_code=pulumi_code,
        stack_name="CI",
        project_name="ol-test",
        project_source_path=Path("src/ol_infrastructure/test"),
    )
    assert [step.attempts for step in _pulumi_put_steps(fragment)] == [2]


def test_pulumi_job_does_not_retry(pulumi_code):
    fragment = pulumi_job(
        pulumi_code=pulumi_code,
        stack_name="CI",
        project_name="ol-test",
        project_source_path=Path("src/ol_infrastructure/test"),
    )
    steps = _pulumi_put_steps(fragment)
    assert steps, "expected a pulumi-provisioner put step"
    assert all(step.attempts is None for step in steps)


def test_pulumi_jobs_chain_does_not_retry_any_stage(pulumi_code):
    fragment = pulumi_jobs_chain(
        pulumi_code=pulumi_code,
        stack_names=["CI", "QA", "Production"],
        project_name="ol-test",
        project_source_path=Path("src/ol_infrastructure/test"),
    )
    steps = _pulumi_put_steps(fragment)
    assert len(steps) == 3, "expected one pulumi put per stack"
    assert all(step.attempts is None for step in steps)


def _keys_anywhere(node):
    """Every mapping key in a nested dict/list structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys_anywhere(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys_anywhere(item)


def test_no_retry_wrapper_is_serialized(pulumi_code):
    """``attempts=None`` must drop out of the rendered pipeline, not render as 1."""
    fragment = pulumi_job(
        pulumi_code=pulumi_code,
        stack_name="CI",
        project_name="ol-test",
        project_source_path=Path("src/ol_infrastructure/test"),
    )
    rendered = fragment.jobs[0].model_dump(exclude_none=True)
    assert "attempts" not in set(_keys_anywhere(rendered))


def test_promotion_gate_still_hangs_off_on_success(pulumi_code):
    """The gate issue must stay gated on success -- it is the promotion trigger."""
    fragment = pulumi_jobs_chain(
        pulumi_code=pulumi_code,
        stack_names=["CI", "QA"],
        project_name="ol-test",
        project_source_path=Path("src/ol_infrastructure/test"),
    )
    ci_job = fragment.jobs[0]
    assert isinstance(ci_job.on_success, PutStep)
    assert "github-issues" in ci_job.on_success.put
    assert ci_job.ensure is None, "the gate must not post unconditionally"
