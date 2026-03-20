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

_AWS_ACCOUNT_ID = "610119931565"
_AWS_REGION = "us-east-1"
_BASE_IMAGE_REPO = "mitodl/xqueue-watcher-grader-base"


def grader_base_image_pipeline() -> Pipeline:
    """Return the pipeline that builds and publishes the grader base image."""
    xqwatcher_repo = git_repo(
        name=Identifier("xqueue-watcher-code"),
        uri="https://github.com/mitodl/xqueue-watcher",
        branch="main",
        paths=["grader_support/"],
    )

    # DockerHub push target — public, used by grader repo Dockerfiles as default
    # GRADER_BASE_IMAGE build arg and accessible without AWS credentials.
    dockerhub_base_image = registry_image(
        name=Identifier("grader-base-dockerhub"),
        image_repository=_BASE_IMAGE_REPO,
        image_tag="latest",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    # ECR push target — used as the trigger source for per-grader build
    # pipelines so that a base image rebuild causes downstream rebuilds.
    ecr_base_image = registry_image(
        name=Identifier("grader-base-ecr"),
        image_repository=_BASE_IMAGE_REPO,
        image_tag="latest",
        ecr_region=_AWS_REGION,
    )

    build_job = Job(
        name=Identifier("build-grader-base-image"),
        plan=[
            GetStep(get=xqwatcher_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=xqwatcher_repo.name)],
                build_parameters={
                    "CONTEXT": str(xqwatcher_repo.name),
                    "DOCKERFILE": (
                        f"{xqwatcher_repo.name}/grader_support/Dockerfile.base"
                    ),
                },
            ),
            ensure_ecr_task(_BASE_IMAGE_REPO),
            # Push to DockerHub first — fail fast if credentials are wrong
            # before consuming the ECR push quota.
            PutStep(
                put=dockerhub_base_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
            PutStep(
                put=ecr_base_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    fragment = PipelineFragment(
        resources=[xqwatcher_repo, dockerhub_base_image, ecr_base_image],
        jobs=[build_job],
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
