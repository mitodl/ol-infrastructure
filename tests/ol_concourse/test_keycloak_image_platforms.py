"""Guard the mitodl/keycloak image's platforms.

Apple-silicon local-dev clusters pull this image. An OCI index without an
arm64 entry fails there with "no match for platform in manifest", so the
build must not switch to OCI output while dropping arm64. A multi-platform
build writes an OCI layout directory, and the put has to read that instead of
image.tar.
"""

import json

from ol_concourse.pipelines.infrastructure.keycloak.pipeline import (
    build_keycloak_infrastructure_pipeline,
)


def test_keycloak_image_is_multi_arch_and_pushes_the_layout_directory():
    pipeline = json.loads(
        build_keycloak_infrastructure_pipeline().to_pipeline().model_dump_json()
    )
    (job,) = [j for j in pipeline["jobs"] if j["name"] == "build-keycloak-docker-image"]
    (build,) = [s for s in job["plan"] if s.get("task") == "build-container-image"]
    (put,) = [s for s in job["plan"] if s.get("put") == "keycloak-image"]

    platforms = set(build["config"]["params"]["IMAGE_PLATFORM"].split(","))
    assert platforms == {"linux/amd64", "linux/arm64"}
    assert put["params"]["image"] == "image/image"
