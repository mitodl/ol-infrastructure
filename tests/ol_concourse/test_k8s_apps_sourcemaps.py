"""The Sentry source map upload is its own job, fed by the pushed release image.

The job must not sit on any deploy job's ``passed`` list (a failed upload
cannot hold a release), must read the maps out of the image the registry-image
resource unpacks on ``get`` (so the build needs no UNPACK_ROOTFS), and must
report failure through Slack rather than a swallowed ``try``.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from bridge.settings.sentry import MIT_LEARN_SENTRY_PROJECT_SLUG, SENTRY_ORG_SLUG
from ol_concourse.pipelines.infrastructure.k8s_apps.meta import meta_pipeline
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    SOURCEMAPS_UPLOAD_TASK_NAME,
    _build_release_resource_app_pipeline,
    build_app_pipeline,
    pipeline_params,
)

APP = "mit-learn-nextjs"
UPLOAD_JOB = f"upload-{APP}-sourcemaps"
IMAGE_RESOURCE = f"{APP}-app-release-ecr-image"
RELEASE_RESOURCE = f"{APP}-release"
LEGACY_BUILD_JOB = f"build-{APP}-image-from-release_candidate"
RELEASE_BUILD_JOB = f"build-{APP}-release-image"
SENTRY_PROGRAM = (
    Path(__file__).parents[2]
    / "src/ol_infrastructure/infrastructure/sentry/__main__.py"
)


def _legacy() -> dict[str, Any]:
    return json.loads(build_app_pipeline(APP).model_dump_json(exclude_none=True))


def _release() -> dict[str, Any]:
    pipeline = _build_release_resource_app_pipeline(APP, pipeline_params[APP])
    return json.loads(pipeline.model_dump_json(exclude_none=True))


def _job(pipeline: dict[str, Any], name: str) -> dict[str, Any]:
    (job,) = [j for j in pipeline["jobs"] if j["name"] == name]
    return job


SHAPES = pytest.mark.parametrize(
    ("pipeline", "build_job"),
    [(_legacy(), LEGACY_BUILD_JOB), (_release(), RELEASE_BUILD_JOB)],
    ids=["legacy", "release-resource"],
)


@SHAPES
def test_upload_job_reads_every_pushed_image(pipeline, build_job):
    """The job triggers on each pushed image and uploads from its unpacked rootfs."""
    job = _job(pipeline, UPLOAD_JOB)
    image_get = job["plan"][0]

    assert image_get["get"] == IMAGE_RESOURCE
    assert image_get["trigger"] is True
    assert image_get["version"] == "every"
    assert image_get["passed"] == [build_job]
    assert image_get["params"] == {"format": "rootfs"}

    (upload,) = [s for s in job["plan"] if s.get("task") == SOURCEMAPS_UPLOAD_TASK_NAME]
    args = upload["config"]["run"]["args"]
    assert args[: args.index("--release")] == [
        "sourcemaps",
        "upload",
        "--org",
        SENTRY_ORG_SLUG,
        "--project",
        MIT_LEARN_SENTRY_PROJECT_SLUG,
    ]
    assert args[args.index("--release") + 1] == "((.:release_version))"
    assert args[-1] == f"{IMAGE_RESOURCE}/rootfs/app/frontends/main/.next"
    assert upload["config"]["inputs"] == [{"name": IMAGE_RESOURCE}]
    assert "SENTRY_AUTH_TOKEN" in upload["config"]["params"]


def test_legacy_release_version_is_the_image_tag():
    """On the legacy flow the RC image's tag is the release version."""
    job = _job(_legacy(), UPLOAD_JOB)
    (load_var,) = [s for s in job["plan"] if s.get("load_var") == "release_version"]
    assert load_var["file"] == f"{IMAGE_RESOURCE}/tag"


def test_release_resource_version_comes_from_the_release_at_the_same_build():
    """On the release flow the version comes from the release resource at the build."""
    job = _job(_release(), UPLOAD_JOB)
    (release_get,) = [s for s in job["plan"] if s.get("get") == RELEASE_RESOURCE]
    assert release_get["passed"] == [RELEASE_BUILD_JOB]
    (load_var,) = [s for s in job["plan"] if s.get("load_var") == "release_version"]
    assert load_var["file"] == f"{RELEASE_RESOURCE}/version"


@SHAPES
def test_upload_failure_alerts_slack_and_blocks_no_deploy(pipeline, build_job):  # noqa: ARG001 -- SHAPES supplies both
    """A failed upload alerts Slack and no deploy job waits on it."""
    job = _job(pipeline, UPLOAD_JOB)

    assert all("try" not in step for step in job["plan"])
    for hook in ("on_failure", "on_error"):
        assert job[hook]["put"] == f"slack-alert-{APP}-sourcemaps"
    assert job["on_failure"]["params"]["alert_type"] == "failed"
    assert any(
        r["name"] == f"slack-alert-{APP}-sourcemaps"
        and r["type"] == "slack-notification"
        for r in pipeline["resources"]
    )

    dependents = [
        other["name"]
        for other in pipeline["jobs"]
        if other["name"] != UPLOAD_JOB
        for step in other["plan"]
        if UPLOAD_JOB in (step.get("passed") or [])
    ]
    assert dependents == []


@SHAPES
def test_build_jobs_carry_no_sentry_token(pipeline, build_job):
    """The Sentry token appears nowhere in the image build job."""
    build = json.dumps(_job(pipeline, build_job))
    assert "SENTRY_AUTH_TOKEN" not in build
    assert pipeline_params[APP].sentry_sourcemaps.auth_token_vault_key not in build


def test_editing_the_sentry_slugs_regenerates_the_pipeline():
    """The slugs are baked into the generated pipeline, so meta has to watch them."""
    meta = meta_pipeline([APP]).model_dump()
    (definitions,) = [
        r for r in meta["resources"] if r["name"] == "k8s-app-pipeline-definitions"
    ]

    assert "src/bridge/settings/sentry.py" in {
        str(path) for path in definitions["source"]["paths"]
    }


def test_slugs_match_the_sentry_pulumi_program():
    """The Pulumi program is regenerated from live Sentry, so it is the truth.

    sentry-cli fails with ``One or more projects are invalid`` when the slug it
    is given is not a live project's slug, and only after the image is pushed.
    The regeneration also renames each project's Pulumi variable after its
    slug, so this keys on the ``slug=`` arguments, not the variable names.
    """
    source = SENTRY_PROGRAM.read_text()
    project_slugs = re.findall(
        r"sentry\.SentryProject\((?:(?!\n\)).)*?slug=['\"]([^'\"]+)['\"]",
        source,
        re.S,
    )
    assert project_slugs, "no sentry.SentryProject blocks found in the Sentry program"
    assert MIT_LEARN_SENTRY_PROJECT_SLUG in project_slugs

    org_slugs = re.findall(
        r"sentry\.SentryOrganization\((?:(?!\n\)).)*?slug=['\"]([^'\"]+)['\"]",
        source,
        re.S,
    )
    assert org_slugs == [SENTRY_ORG_SLUG]
