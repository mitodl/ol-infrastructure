"""Guard the ``production/<version>`` tag a k8s release's Production job writes.

The tag records that a release reached Production. The API-client pipelines of
mitxonline and ol-analytics-api watch it, so a stable client is published only
for a promoted release (libraries/configuration.py). These tests pin that every
k8s release-resource app writes it, last in the Production job and only after
the deploy is recorded and the release finished, inside a ``try`` that alerts
Slack rather than failing a deploy that succeeded, and that a legacy app does not.
``promote_tag.py`` itself is tested in test_k8s_apps_tag_release.py.
"""

import json
from typing import Any

import pytest

from bridge.settings.apps import (
    APPS,
    PRODUCTION_TAG_PREFIX,
    release_kind,
    release_resource_workflow,
)
from bridge.settings.apps import github_repo as app_github_repo
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    CHECK_PROMOTE_TAG_TASK_NAME,
    GITHUB_APP_ID,
    GITHUB_APP_INSTALLATION_ID,
    GITHUB_APP_PRIVATE_KEY,
    PROMOTE_TAG_TASK_NAME,
    build_app_pipeline,
)
from ol_concourse.pipelines.libraries.configuration import (
    PIPELINE_CONFIGS,
    PRODUCTION_RELEASE_TAG_REGEX,
)

K8S_RELEASE_APPS = [
    app for app in APPS if release_resource_workflow(app) and release_kind(app) == "k8s"
]
LEGACY_APPS = [app for app in APPS if not release_resource_workflow(app)]


def _production_plan(app_name: str) -> list[dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [
        job
        for job in pipeline["jobs"]
        if job["name"] == f"deploy-ol-application-{app_name}-production"
    ]
    return job["plan"]


def _step_names(plan: list[dict[str, Any]]) -> list[str]:
    names = []
    for step in plan:
        inner = step.get("try", step)
        names.append(inner.get("task") or inner.get("put") or inner.get("get") or "")
    return names


def test_the_api_client_sources_are_k8s_release_apps():
    """The tag-driven API-client pipelines need a Production job that tags."""
    for variant, config in PIPELINE_CONFIGS.items():
        if config.get("source_repo_tag_regex") == PRODUCTION_RELEASE_TAG_REGEX:
            assert variant in K8S_RELEASE_APPS


@pytest.mark.parametrize("app_name", K8S_RELEASE_APPS)
def test_production_tags_the_release_last(app_name):
    plan = _production_plan(app_name)
    names = _step_names(plan)
    deployment = f"{app_name}-deployment-production"
    release = f"{app_name}-release"
    recorded = len(names) - 1 - names[::-1].index(deployment)
    finished = len(names) - 1 - names[::-1].index(release)

    assert names[-1] == PROMOTE_TAG_TASK_NAME
    assert names.count(PROMOTE_TAG_TASK_NAME) == 1
    assert plan[recorded]["params"]["state"] == "success"
    assert plan[finished]["params"]["action"] == "finish"
    assert recorded < finished < len(names) - 1


@pytest.mark.parametrize("app_name", K8S_RELEASE_APPS)
def test_a_failed_production_tag_alerts_instead_of_failing_the_deploy(app_name):
    """The release is live by then. A red job would read as a failed deploy
    and withhold the deployment from jobs that take it ``passed:`` this one
    (learn-ai's Production frontend). The inner task's hooks say a client
    will not be published.
    """
    step = _production_plan(app_name)[-1]
    inner = step["try"]

    assert inner["task"] == PROMOTE_TAG_TASK_NAME
    for hook in ("on_failure", "on_error"):
        alert = inner[hook]
        assert alert["put"] == f"slack-alert-{app_name}-production-tag"
        assert alert["no_get"] is True
        assert "no API client is published" in alert["params"]["text"]


@pytest.mark.parametrize("app_name", K8S_RELEASE_APPS)
def test_production_tags_the_version_it_deployed(app_name):
    task = _production_plan(app_name)[-1]["try"]
    params = task["config"]["params"]

    assert params == {
        "VERSION_FILE": f"{app_name}-release/version",
        "TAG_PREFIX": PRODUCTION_TAG_PREFIX,
        "GITHUB_REPOSITORY": app_github_repo(app_name),
        "GITHUB_APP_ID": GITHUB_APP_ID,
        "GITHUB_APP_INSTALLATION_ID": GITHUB_APP_INSTALLATION_ID,
        "GITHUB_APP_PRIVATE_KEY": GITHUB_APP_PRIVATE_KEY,
        "ALLOW_OLDER": "true",
    }
    assert task["config"]["inputs"] == [{"name": f"{app_name}-release"}]


@pytest.mark.parametrize("app_name", K8S_RELEASE_APPS)
def test_production_does_not_refuse_a_rollback_before_deploying(app_name):
    """Unlike a tag-only release, the check that refuses an older version
    does not run before the deploy, so re-running an old build still rolls
    Production back.
    """
    assert CHECK_PROMOTE_TAG_TASK_NAME not in _step_names(_production_plan(app_name))


@pytest.mark.parametrize("app_name", LEGACY_APPS)
def test_a_legacy_app_writes_no_production_tag(app_name):
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    for job in pipeline["jobs"]:
        assert PROMOTE_TAG_TASK_NAME not in _step_names(job["plan"])
