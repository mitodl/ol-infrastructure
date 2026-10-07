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
    build_app_pipeline,
)

APP = "micromasters"


def _job(app_name: str, job_name: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"] == job_name]
    return job


@pytest.mark.parametrize(
    "job_name",
    [f"build-{APP}-image-from-master", f"build-{APP}-release-image"],
)
def test_image_builds_output_oci_and_push_the_layout_directory(job_name):
    plan = _job(APP, job_name)["plan"]
    (build,) = [step for step in plan if step.get("task") == "build-container-image"]
    puts = [step for step in plan if f"{APP}-app" in step.get("put", "")]

    assert build["config"]["params"]["OUTPUT_OCI"] == "true"
    assert len(puts) == 2
    assert {put["params"]["image"] for put in puts} == {"image/image"}
