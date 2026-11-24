"""Guard the end-to-end test jobs in the edxapp pipeline.

Each target gets two jobs running `openedx/end-to-end-tests` against its stage:
one after that stage's deploy, one nightly. They are a signal, not a gate: if any
job ever took `passed` from them, a red run (an RC-only failure, a flaky test, a
suite change) would hold up the next stage's preview and deploy.
"""

import json
import re
from typing import Any

import pytest

from ol_concourse.pipelines.open_edx.edx_platform_v3.e2e import (
    E2E_SUITE_REF,
    E2E_TARGETS,
    stage_deploy_job,
)
from ol_concourse.pipelines.open_edx.edx_platform_v3.pipeline import (
    build_edx_pipeline,
)
from tests.ol_concourse._build_manifest import RELEASES, TOPOLOGY, write_manifest
from tests.ol_concourse._plan import walk

# Every e2e job: each target's post-deploy job and its nightly one.
E2E_JOBS = [
    pytest.param(target, name, id=name)
    for target in E2E_TARGETS
    for name in (target.job_name, f"{target.job_name}-nightly")
]
E2E_JOB_NAMES = [param.values[1] for param in E2E_JOBS]


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    manifest = write_manifest(tmp_path_factory.mktemp("lehrer"), TOPOLOGY)
    rendered = build_edx_pipeline(RELEASES, manifest)
    return json.loads(rendered.model_dump_json(by_alias=True, exclude_none=True))


def _job(pipeline: dict[str, Any], name: str) -> dict[str, Any]:
    (job,) = [job for job in pipeline["jobs"] if job["name"] == name]
    return job


def _gets(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["get"]: step for step in walk(job["plan"]) if "get" in step}


def _nightly_resource(target) -> str:
    return f"e2e-nightly-{target.deployment_name}-{target.stage.lower()}"


@pytest.mark.parametrize("job_name", E2E_JOB_NAMES)
def test_no_job_waits_on_the_e2e_job(pipeline, job_name):
    for job in pipeline["jobs"]:
        for step in walk(job["plan"]):
            assert job_name not in step.get("passed", []), (
                f"{job['name']} takes `passed` from {job_name}, so a red "
                "e2e run would block it"
            )


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_post_deploy_job_runs_after_the_stage_deploy(pipeline, target):
    """It must not also wait for a schedule version.

    A time resource with a start/stop window has no version until its first
    window opens, and a job cannot schedule while any input lacks one: a schedule
    get here would hold every post-deploy run on a new pipeline until that tick.
    """
    deploy_suffix = f"-{target.deployment_name}-{target.stage}".lower()
    gets = _gets(_job(pipeline, target.job_name))

    assert _nightly_resource(target) not in gets
    assert gets, "the job takes nothing the deploy job deployed"
    for step in gets.values():
        (deploy_job,) = step["passed"]
        assert deploy_job.startswith("deploy-")
        assert deploy_job.endswith(deploy_suffix)
        assert step["trigger"] is True


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_nightly_job_runs_on_the_schedule_alone(pipeline, target):
    gets = _gets(_job(pipeline, f"{target.job_name}-nightly"))

    assert gets[_nightly_resource(target)]["trigger"] is True
    assert [name for name, step in gets.items() if step.get("trigger")] == [
        _nightly_resource(target)
    ]


@pytest.mark.parametrize("target", E2E_TARGETS, ids=lambda t: t.job_name)
def test_post_deploy_and_nightly_runs_never_overlap(pipeline, target):
    """Both share the stage's courses, users and site-wide switches."""
    post_deploy = _job(pipeline, target.job_name)
    nightly = _job(pipeline, f"{target.job_name}-nightly")

    assert post_deploy.get("serial_groups")
    assert post_deploy["serial_groups"] == nightly.get("serial_groups")


def _task(pipeline: dict[str, Any], job_name: str) -> dict[str, Any]:
    (task,) = [
        step for step in walk(_job(pipeline, job_name)["plan"]) if "task" in step
    ]
    return task


@pytest.mark.parametrize(("target", "job_name"), E2E_JOBS)
def test_admin_credential_comes_from_vault(pipeline, target, job_name):
    params = _task(pipeline, job_name)["config"]["params"]
    assert params["ADMIN_USERNAME"] == f"(({target.admin_credential}.username))"
    assert params["ADMIN_PASSWORD"] == f"(({target.admin_credential}.password))"


@pytest.mark.parametrize("job_name", E2E_JOB_NAMES)
def test_runs_the_pinned_suite_commit(pipeline, job_name):
    """A new commit to the suite's main must not run here before it is reviewed."""
    assert re.fullmatch(r"[0-9a-f]{40}", E2E_SUITE_REF)
    script = _task(pipeline, job_name)["config"]["run"]["args"][-1]
    assert f"git checkout --quiet {E2E_SUITE_REF}" in script
    assert "end-to-end-tests" not in _gets(_job(pipeline, job_name))


@pytest.mark.parametrize(("target", "job_name"), E2E_JOBS)
def test_suite_looks_for_the_stage_jwt_cookie(pipeline, target, job_name):
    """The suite looks for the cookie under the name this stage gives it.

    edxapp renames it per stack (secrets_builder.py's ``<env_prefix>-<env_suffix>-
    edx-jwt-cookie-header-payload``); under Open edX's default name no sign-in
    passes. The pinned suite reads the name from this setting, unpatched.
    """
    expected = (
        f"{target.deployment_name}-{target.stage.lower()}-edx-jwt-cookie-header-payload"
    )
    task = _task(pipeline, job_name)
    assert task["config"]["params"]["JWT_AUTH_COOKIE_HEADER_PAYLOAD"] == expected
    assert "preflight.ts" not in task["config"]["run"]["args"][-1]


@pytest.mark.parametrize("job_name", E2E_JOB_NAMES)
def test_nothing_is_uploaded(pipeline, job_name):
    """Reports and traces hold the admin sign-in, so they never leave the task."""
    job = _job(pipeline, job_name)
    task = _task(pipeline, job_name)
    assert not [step for step in walk(job["plan"]) if "put" in step]
    assert "outputs" not in task["config"]
    assert not [
        hook
        for hook in ("ensure", "on_success", "on_failure", "on_error")
        if hook in task
    ]


@pytest.mark.parametrize("job_name", E2E_JOB_NAMES)
def test_job_is_in_its_release_group(pipeline, job_name):
    groups = [group for group in pipeline["groups"] if job_name in group["jobs"]]
    assert len(groups) == 1


@pytest.mark.parametrize(
    "job_names",
    [
        ["deploy-ol-infrastructure-edxapp-application.mitxonline-mitxonline-ci"],
        [
            "deploy-ol-infrastructure-edxapp-application.mitxonline-mitxonline-qa",
            "deploy-other-project.mitxonline-mitxonline-qa",
        ],
    ],
    ids=["no-match", "two-matches"],
)
def test_names_the_stage_when_its_deploy_job_is_ambiguous(job_names):
    """A target whose stage maps to no deploy job, or to several, says which."""
    with pytest.raises(ValueError, match=r"mitxonline.*QA"):
        stage_deploy_job(job_names, "mitxonline", "QA")
