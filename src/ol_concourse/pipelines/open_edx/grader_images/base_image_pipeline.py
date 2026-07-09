"""
Pipeline that builds the xqueue-watcher grader base image and pushes it to
both DockerHub and ECR.

The base image (grader_support/Dockerfile.base) is the foundation for all
course-specific grader images.  Publishing it to both registries allows:
  - DockerHub (mitodl/xqueue-watcher-grader-base): public reference usable
    without AWS credentials; used in grader repo Dockerfiles as the default
    GRADER_BASE_IMAGE build arg.  The per-grader Concourse build pipelines
    trigger off this DockerHub image so a base image rebuild automatically
    triggers downstream grader image rebuilds.
  - ECR (mitodl/xqueue-watcher-grader-base): private mirror for use inside
    AWS without DockerHub rate-limit concerns.

Dockerfile.base exposes ``ARG PYTHON_VERSION=3.12``, so one build job per
supported interpreter is run, each tagging its image with the Python version
(e.g. ``3.14``).  The default version's job additionally publishes a
``:latest`` tag so existing per-grader pipelines (build_pipeline.py), which
still trigger off ``grader_base_dockerhub_repo``'s default ``latest`` tag,
keep working unchanged.

Triggers:
  - Push to the xqueue-watcher repo on paths under grader_support/.
"""

import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

_AWS_REGION = "us-east-1"
_BASE_IMAGE_REPO = "mitodl/xqueue-watcher-grader-base"
_PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
DEFAULT_PYTHON_VERSION = "3.12"  # matches Dockerfile.base's ARG default


def _version_slug(python_version: str) -> str:
    return python_version.replace(".", "")


def grader_base_image_pipeline() -> Pipeline:
    """Return the pipeline that builds and publishes the grader base image."""
    xqwatcher_repo = git_repo(
        name=Identifier("xqueue-watcher-code"),
        uri="https://github.com/mitodl/xqueue-watcher",
        branch="master",
        paths=["grader_support/"],
    )

    # DockerHub push targets — public, used by grader repo Dockerfiles as
    # default GRADER_BASE_IMAGE build arg and accessible without AWS
    # credentials.  One per Python version in the build matrix.
    dockerhub_base_images = {
        version: registry_image(
            name=Identifier(f"grader-base-dockerhub-py{_version_slug(version)}"),
            image_repository=_BASE_IMAGE_REPO,
            image_tag=version,
            username="((dockerhub.username))",
            password="((dockerhub.password))",  # noqa: S106
        )
        for version in _PYTHON_VERSIONS
    }

    # ECR push targets — private mirror for use inside AWS without DockerHub
    # rate-limit concerns.  The per-grader Concourse build pipelines trigger
    # off the DockerHub base image (grader_base_dockerhub_repo), not ECR.
    ecr_base_images = {
        version: registry_image(
            name=Identifier(f"grader-base-ecr-py{_version_slug(version)}"),
            image_repository=_BASE_IMAGE_REPO,
            image_tag=version,
            ecr_region=_AWS_REGION,
        )
        for version in _PYTHON_VERSIONS
    }

    dockerhub_latest_image = registry_image(
        name=Identifier("grader-base-dockerhub-latest"),
        image_repository=_BASE_IMAGE_REPO,
        image_tag="latest",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )
    ecr_latest_image = registry_image(
        name=Identifier("grader-base-ecr-latest"),
        image_repository=_BASE_IMAGE_REPO,
        image_tag="latest",
        ecr_region=_AWS_REGION,
    )

    def build_job(python_version: str) -> Job:
        plan = [
            GetStep(get=xqwatcher_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=xqwatcher_repo.name)],
                build_parameters={
                    "CONTEXT": str(xqwatcher_repo.name),
                    "DOCKERFILE": (
                        f"{xqwatcher_repo.name}/grader_support/Dockerfile.base"
                    ),
                    "BUILD_ARG_PYTHON_VERSION": python_version,
                },
            ),
            ensure_ecr_task(_BASE_IMAGE_REPO),
            # Push to DockerHub first — fail fast if credentials are wrong
            # before consuming the ECR push quota.
            PutStep(
                put=dockerhub_base_images[python_version].name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
            PutStep(
                put=ecr_base_images[python_version].name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
        ]
        if python_version == DEFAULT_PYTHON_VERSION:
            plan.extend(
                [
                    PutStep(
                        put=dockerhub_latest_image.name,
                        params={"image": "image/image.tar"},
                    ),
                    PutStep(
                        put=ecr_latest_image.name,
                        params={"image": "image/image.tar"},
                    ),
                ]
            )
        return Job(
            name=Identifier(
                f"build-grader-base-image-py{_version_slug(python_version)}"
            ),
            plan=plan,
        )

    fragment = PipelineFragment(
        resources=[
            xqwatcher_repo,
            *dockerhub_base_images.values(),
            *ecr_base_images.values(),
            dockerhub_latest_image,
            ecr_latest_image,
        ],
        jobs=[build_job(version) for version in _PYTHON_VERSIONS],
    )

    return Pipeline(
        resource_types=fragment.resource_types,
        resources=fragment.resources,
        jobs=fragment.jobs,
    )


if __name__ == "__main__":
    pipeline_json = grader_base_image_pipeline().model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p build-grader-base-image -c definition.json\n"
    )
