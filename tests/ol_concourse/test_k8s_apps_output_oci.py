"""Guard OCI output on the release-resource workflow's image builds.

mitodl/ol-python-base inherits zstd-compressed layers from its DHI base.
oci-build-task's default Docker-format tarball can only label a layer as
gzip, so a single-platform build from that base pushes zstd layers labeled
gzip, and the registry-image put's implicit get fails with
``gzip: invalid header``. OUTPUT_OCI keeps the labels correct, and the put
must then read the OCI layout directory rather than the tarball.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    _build_release_resource_app_pipeline,
    build_app_pipeline,
    pipeline_params,
)

APP = "micromasters"
# No release-workflow app uploads Sentry source maps yet, so build
# mit-learn-nextjs's params (which do) through the release-workflow builder.
SOURCEMAPS_APP = "mit-learn-nextjs"


def _job(pipeline_json: str, job_name: str) -> dict[str, Any]:
    pipeline = json.loads(pipeline_json)
    (job,) = [j for j in pipeline["jobs"] if j["name"] == job_name]
    return job


def _build_and_puts(
    job: dict[str, Any], app_name: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = job["plan"]
    (build,) = [step for step in plan if step.get("task") == "build-container-image"]
    puts = [step for step in plan if f"{app_name}-app" in step.get("put", "")]
    return build, puts


@pytest.mark.parametrize(
    "job_name",
    [f"build-{APP}-image-from-master", f"build-{APP}-release-image"],
)
def test_image_builds_output_oci_and_push_the_layout_directory(job_name):
    job = _job(build_app_pipeline(APP).model_dump_json(), job_name)
    build, puts = _build_and_puts(job, APP)

    assert build["config"]["params"]["OUTPUT_OCI"] == "true"
    assert len(puts) == 2
    assert {put["params"]["image"] for put in puts} == {"image/image"}


def test_release_build_with_sourcemaps_keeps_the_tarball():
    """The source-map upload reads the unpacked rootfs, which needs the tarball."""
    pipeline = _build_release_resource_app_pipeline(
        SOURCEMAPS_APP, pipeline_params[SOURCEMAPS_APP]
    )
    job = _job(pipeline.model_dump_json(), f"build-{SOURCEMAPS_APP}-release-image")
    build, puts = _build_and_puts(job, SOURCEMAPS_APP)

    assert "OUTPUT_OCI" not in build["config"]["params"]
    assert build["config"]["params"]["UNPACK_ROOTFS"] == "true"
    assert len(puts) == 2
    assert {put["params"]["image"] for put in puts} == {"image/image.tar"}
