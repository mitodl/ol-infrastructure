"""An app's static frontend ships with its release, not from release branches.

learn-ai's ``frontend-demo/`` used to be deployed by a separate pipeline that
watched the ``release-candidate`` and ``release`` branches. The release-resource
workflow advances neither, so that pipeline's QA resource failed to check and
its Production resource sat on a commit from before the migration. The frontend
jobs now live in the app's own pipeline and follow its GitHub Deployments.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    AppPipelineParams,
    build_app_pipeline,
    pipeline_params,
)

APP = "learn-ai"
BRANCH_NAMES = ("release", "release-candidate")


def _pipeline(app_name: str = APP) -> dict[str, Any]:
    return json.loads(build_app_pipeline(app_name).model_dump_json())


def _job(name: str) -> dict[str, Any]:
    (job,) = [j for j in _pipeline()["jobs"] if j["name"] == name]
    return job


def _gets(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["get"]: step for step in job["plan"] if "get" in step}


def test_every_environment_gets_a_frontend_job():
    """Each of CI, QA and Production has its own deploy job."""
    names = {j["name"] for j in _pipeline()["jobs"]}

    assert {
        f"deploy-{APP}-frontend-ci",
        f"deploy-{APP}-frontend-qa",
        f"deploy-{APP}-frontend-production",
    } <= names


@pytest.mark.parametrize(
    ("stage", "deployment", "deploy_job"),
    [
        ("qa", f"{APP}-deployment-rc", f"deploy-ol-application-{APP}-qa"),
        (
            "production",
            f"{APP}-deployment-production",
            f"deploy-ol-application-{APP}-production",
        ),
    ],
)
def test_gated_frontend_runs_only_after_the_backend_deploy(
    stage, deployment, deploy_job
):
    """The Deployment is the trigger, and only a version that passed the deploy."""
    (get,) = _gets(_job(f"deploy-{APP}-frontend-{stage}")).values()

    assert get["get"] == deployment
    assert get["trigger"] is True
    assert get["passed"] == [deploy_job]


@pytest.mark.parametrize("stage", ["qa", "production"])
def test_gated_frontend_builds_the_release_tag_from_the_deployment(stage):
    """The tag comes out of deployment.json, which is read before the checkout."""
    plan = _job(f"deploy-{APP}-frontend-{stage}")["plan"]
    load = next(s for s in plan if s.get("load_var") == "deployment")
    checkout = next(s for s in plan if s.get("task") == "checkout-frontend-release")

    assert load["file"].endswith("/deployment.json")
    assert load["format"] == "json"
    assert plan.index(load) < plan.index(checkout)
    assert checkout["config"]["params"]["VERSION"] == "((.:deployment.ref))"
    build = next(s for s in plan if s.get("task", "").startswith("build-"))
    assert [i["name"] for i in build["config"]["inputs"]] == ["frontend-release-source"]


def test_ci_frontend_tracks_the_frontend_path_on_main():
    """CI is the one job that follows a branch, and only for the frontend path."""
    job = _job(f"deploy-{APP}-frontend-ci")
    (get,) = _gets(job).values()
    (resource,) = [r for r in _pipeline()["resources"] if r["name"] == get["get"]]

    assert get["trigger"] is True
    assert resource["source"]["branch"] == "main"
    assert resource["source"]["paths"] == ["frontend-demo"]


def test_each_environment_syncs_its_own_bucket_and_purges_its_own_fastly():
    pipeline = _pipeline()
    for stage, bucket in [
        ("ci", "ol-mit-learn-ai-ci"),
        ("qa", "ol-mit-learn-ai-qa"),
        ("production", "ol-mit-learn-ai-production"),
    ]:
        puts = {
            s["put"]: s["params"]
            for s in _job(f"deploy-{APP}-frontend-{stage}")["plan"]
            if "put" in s
        }
        (sync,) = puts[f"{APP}-frontend-bucket"]["destination"]
        assert sync["dir"] == f"s3-remote:{bucket}/frontend/"
        assert puts[f"{APP}-frontend-fastly-{stage}"] == {"mode": "purge_all"}
    assert {r["type"] for r in pipeline["resources"] if "frontend" in r["name"]} == {
        "rclone",
        "fastly",
        "git",
    }


def test_nothing_in_the_frontend_watches_a_release_branch():
    """The regression: a git resource on release-candidate/release never moves."""
    pipeline = _pipeline()
    frontend = [r for r in pipeline["resources"] if "frontend" in r["name"]]

    assert frontend
    assert not [
        r["name"] for r in frontend if r.get("source", {}).get("branch") in BRANCH_NAMES
    ]


def test_apps_without_a_frontend_are_unchanged():
    for app in pipeline_params:
        if app == APP:
            continue
        names = {j["name"] for j in _pipeline(app)["jobs"]}
        assert not [n for n in names if "-frontend-" in n]


def test_a_legacy_app_cannot_carry_a_frontend():
    """The legacy shape has no Deployments, so it would silently drop the jobs."""
    params = AppPipelineParams(
        app_name="mitxonline",
        static_frontend=pipeline_params[APP].static_frontend,
    )
    with pytest.raises(ValueError, match="static_frontend requires"):
        _build_pipeline_for(params)


def _build_pipeline_for(params: AppPipelineParams):
    pipeline_params[params.app_name], original = (
        params,
        pipeline_params[params.app_name],
    )
    try:
        return build_app_pipeline(params.app_name)
    finally:
        pipeline_params[params.app_name] = original
