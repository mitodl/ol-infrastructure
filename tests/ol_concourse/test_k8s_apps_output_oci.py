"""Guard OCI output on the k8s_apps image builds.

mitodl/ol-python-base inherits zstd-compressed layers from its DHI base.
oci-build-task's default Docker-format tarball can only label a layer as
gzip, so a single-platform build from that base pushes zstd layers labeled
gzip, and the registry-image put's implicit get fails with
``gzip: invalid header``. OUTPUT_OCI keeps the labels correct, and the put
must then read the OCI layout directory rather than the tarball.

Every build gets OUTPUT_OCI, including an app whose source maps go to Sentry:
that upload reads the pushed image back through the registry-image resource
(see test_k8s_apps_sourcemaps.py), so no build needs UNPACK_ROOTFS, which
oci-build-task honours only for its legacy tarball output.
"""

import json
from typing import Any

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    build_app_pipeline,
)

APP = "micromasters"
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


def _assert_oci_output(build: dict[str, Any], puts: list[dict[str, Any]]) -> None:
    assert build["config"]["params"]["OUTPUT_OCI"] == "true"
    assert "UNPACK_ROOTFS" not in build["config"]["params"]
    assert len(puts) == 2
    assert {put["params"]["image"] for put in puts} == {"image/image"}


@pytest.mark.parametrize(
    "job_name",
    [f"build-{APP}-image-from-master", f"build-{APP}-release-image"],
)
def test_image_builds_output_oci_and_push_the_layout_directory(job_name):
    """Release-resource builds output OCI and push the layout directory."""
    job = _job(build_app_pipeline(APP).model_dump_json(), job_name)
    _assert_oci_output(*_build_and_puts(job, APP))


def test_legacy_rc_build_with_sourcemaps_outputs_oci():
    """The legacy RC build outputs OCI too, even for an app with a Sentry upload."""
    job = _job(
        build_app_pipeline(SOURCEMAPS_APP).model_dump_json(),
        f"build-{SOURCEMAPS_APP}-image-from-release_candidate",
    )
    _assert_oci_output(*_build_and_puts(job, SOURCEMAPS_APP))
