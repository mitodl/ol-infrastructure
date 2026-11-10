"""Guard the end-to-end test job in the edxapp pipeline.

The job runs `openedx/end-to-end-tests` against a stage after that stage's deploy,
and nightly. It is a signal, not a gate: if any job ever took `passed` from it, a
red run (an RC-only failure, a flaky test, a suite change) would hold up the next
stage's preview and deploy.
"""

import json
import re
from typing import Any

import pytest

from bridge.settings.openedx.types import OpenEdxSupportedRelease
from ol_concourse.pipelines.open_edx.edx_platform_v3.e2e import (
    E2E_SUITE_REF,
    E2E_TARGETS,
)
from ol_concourse.pipelines.open_edx.edx_platform_v3.pipeline import (
    build_edx_pipeline,
)


@pytest.fixture(scope="module")
def pipeline() -> dict[str, Any]:
    rendered = build_edx_pipeline([release.name for release in OpenEdxSupportedRelease])
    return json.loads(rendered.model_dump_json(by_alias=True, exclude_none=True))


def _steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every step in a plan, nested in_parallel/do steps included."""
    found = []
    for step in plan:
        found.append(step)
        for key in ("in_parallel", "do"):
            nested = step.get(key)
            if isinstance(nested, dict):
                nested = nested.get("steps", [])
            if isinstance(nested, list):
                found.extend(_steps(nested))
    return found


def _job(pipeline: dict[str, Any], name: str) -> dict[str, Any]:
    (job,) = [job for job in pipeline["jobs"] if job["name"] == name]
    return job


def _gets(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["get"]: step for step in _steps(job["plan"]) if "get" in step}


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_no_job_waits_on_the_e2e_job(pipeline, target):
    for job in pipeline["jobs"]:
        for step in _steps(job["plan"]):
            assert target.job_name not in step.get("passed", []), (
                f"{job['name']} takes `passed` from {target.job_name}, so a red "
                "e2e run would block it"
            )


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_runs_after_the_stage_deploy_and_nightly(pipeline, target):
    deploy_suffix = f"-{target.deployment_name}-{target.stage}".lower()
    gets = _gets(_job(pipeline, target.job_name))

    deployed = [step for step in gets.values() if step.get("passed")]
    assert deployed, "the job takes nothing the deploy job deployed"
    for step in deployed:
        (deploy_job,) = step["passed"]
        assert deploy_job.startswith("deploy-")
        assert deploy_job.endswith(deploy_suffix)
        assert step["trigger"] is True

    nightly = f"e2e-nightly-{target.deployment_name}-{target.stage.lower()}"
    assert gets[nightly]["trigger"] is True


def _task(pipeline: dict[str, Any], job_name: str) -> dict[str, Any]:
    (task,) = [
        step for step in _steps(_job(pipeline, job_name)["plan"]) if "task" in step
    ]
    return task


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_admin_credential_comes_from_vault(pipeline, target):
    params = _task(pipeline, target.job_name)["config"]["params"]
    assert params["ADMIN_USERNAME"] == f"(({target.admin_credential}.username))"
    assert params["ADMIN_PASSWORD"] == f"(({target.admin_credential}.password))"


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_runs_the_pinned_suite_commit(pipeline, target):
    """A new commit to the suite's main must not run here before it is reviewed."""
    assert re.fullmatch(r"[0-9a-f]{40}", E2E_SUITE_REF)
    script = _task(pipeline, target.job_name)["config"]["run"]["args"][-1]
    assert f"git checkout --quiet {E2E_SUITE_REF}" in script
    assert "end-to-end-tests" not in _gets(_job(pipeline, target.job_name))


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_nothing_is_uploaded(pipeline, target):
    """Reports and traces hold the admin sign-in, so they never leave the task."""
    job = _job(pipeline, target.job_name)
    assert not [step for step in _steps(job["plan"]) if "put" in step]
    assert "outputs" not in _task(pipeline, target.job_name)["config"]
    assert not [
        hook
        for hook in ("ensure", "on_success", "on_failure", "on_error")
        if hook in _task(pipeline, target.job_name)
    ]


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_job_is_in_its_release_group(pipeline, target):
    groups = [group for group in pipeline["groups"] if target.job_name in group["jobs"]]
    assert len(groups) == 1
