"""
Reusable pipeline builder for course-specific grader images.

Each grader repository (e.g. graders-mit-600x) extends the xqueue-watcher
grader base image with course-specific grader scripts and dependencies.
This module provides a ``GraderPipelineConfig`` dataclass and a
``grader_image_pipeline()`` factory that returns a ``Pipeline`` for building
and pushing that course image to a private ECR repository.

Triggers:
  - New commit to the grader repo (grader scripts or Dockerfile changed).
  - New digest of the Docker Hub grader base image (base image rebuilt /
    security patch applied).

The base image is fetched by Concourse's ``registry-image`` resource (using its
own IAM-based ECR auth) in ``oci`` format, producing a local ``image.tar``.
That tarball is preloaded into the build via ``oci-build-task``'s
``IMAGE_ARG_GRADER_BASE_IMAGE`` parameter, so buildkit resolves the
Dockerfile's ``FROM ${GRADER_BASE_IMAGE}`` from local disk instead of pulling
it from ECR itself.  buildkit has no ECR credentials of its own, so a live
pull returns 401 Unauthorized even though the ``registry-image`` get step,
which authenticates via the worker's IAM role, succeeds.
"""

import dataclasses
import sys

from ol_concourse.lib.containers import ensure_ecr_task
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    Cache,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import registry_image, ssh_git_repo

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

_AWS_ACCOUNT_ID = "610119931565"
_AWS_REGION = "us-east-1"


@dataclasses.dataclass
class GraderPipelineConfig:
    """Parameters for building and publishing one course-specific grader image.

    Attributes:
        pipeline_name: Short identifier used in resource/job names and the
            Concourse pipeline name, e.g. ``"graders-mit-600x"``.
        grader_repo_url: SSH URL of the grader repository, e.g.
            ``"git@github.com:mitodl/graders-mit-600x"``.
        grader_repo_branch: Branch to track, e.g. ``"main"`` or ``"master"``.
        ecr_repo_name: ECR repository name (without the registry host), e.g.
            ``"mitodl/graders-mit-600x"``.  Passed directly to the
            ``registry-image`` resource; ``ecr_region`` causes Concourse to
            infer the correct registry host automatically.
        grader_base_dockerhub_repo: DockerHub repository name for the grader
            base image used as the build trigger, e.g.
            ``"mitodl/xqueue-watcher-grader-base"``.
        github_private_key: Vault path for the SSH private key used to clone
            the (private) grader repository.  Defaults to the odlbot SSH key
            stored at ``infrastructure/open_api_clients`` in Vault.
        aws_account_id: AWS account ID that hosts the ECR registry.
        aws_region: AWS region for ECR authentication.
    """

    pipeline_name: str
    grader_repo_url: str
    grader_repo_branch: str
    ecr_repo_name: str
    grader_base_dockerhub_repo: str = "mitodl/xqueue-watcher-grader-base"
    github_private_key: str = "((open_api_clients.odlbot_private_ssh_key))"
    aws_account_id: str = _AWS_ACCOUNT_ID
    aws_region: str = _AWS_REGION


def grader_image_pipeline(config: GraderPipelineConfig) -> Pipeline:
    """Return a Pipeline that builds and pushes a course-specific grader image.

    The pipeline contains a single build job that:
      1. Watches the grader repo for new commits (trigger).
      2. Watches the grader base image on DockerHub for updates (trigger),
         fetching it in ``oci`` format so it lands on disk as ``image.tar``.
      3. Builds the Dockerfile in the root of the grader repo, preloading the
         fetched ``image.tar`` via ``oci-build-task``'s
         ``IMAGE_ARG_GRADER_BASE_IMAGE`` parameter so buildkit never needs to
         pull the base image (or authenticate to ECR) itself.
      4. Pushes the resulting image to private ECR.

    Args:
        config: Pipeline configuration for the grader repository.

    Returns:
        A ``Pipeline`` object suitable for serialisation to Concourse YAML/JSON.
    """
    grader_repo = ssh_git_repo(
        name=Identifier(f"{config.pipeline_name}-code"),
        uri=config.grader_repo_url,
        branch=config.grader_repo_branch,
        private_key=config.github_private_key,
    )

    # Grader base image via ECR pull-through cache — avoids Docker Hub rate limits.
    # Concourse's periodic `check` on this registry-image resource polls ECR
    # (not push events), which is what prompts pull-through cache refreshes.
    grader_base_image = registry_image(
        name=Identifier("grader-base-image"),
        image_repository=dockerhub_ecr_image_uri(config.grader_base_dockerhub_repo),
        image_tag="latest",
        ecr_region=config.aws_region,
    )

    # Private ECR image for this course's grader.
    grader_ecr_image = registry_image(
        name=Identifier(f"{config.pipeline_name}-image"),
        image_repository=config.ecr_repo_name,
        image_tag="latest",
        ecr_region=config.aws_region,
    )

    # buildkit (running inside oci-build-task) resolves Dockerfile `FROM`
    # references itself and has no ECR credentials of its own — a live pull of
    # a private ECR pull-through-cache repo 401s even though the
    # registry-image get step above (which uses the worker's IAM role) can
    # fetch it fine. Fetching in `oci` format writes an `image.tar` that we
    # preload via IMAGE_ARG_GRADER_BASE_IMAGE, so buildkit resolves the base
    # image from local disk instead of pulling it over the network.
    base_ref = grader_base_image.name
    build_job = Job(
        name=Identifier(f"build-{config.pipeline_name}-image"),
        plan=[
            GetStep(get=grader_repo.name, trigger=True),
            GetStep(
                get=grader_base_image.name,
                trigger=True,
                params={"format": "oci"},
            ),
            TaskStep(
                task=Identifier("build-container-image"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource={
                        "type": "registry-image",
                        "source": {
                            "repository": dockerhub_ecr_image_uri(
                                "concourse/oci-build-task"
                            ),
                            "aws_region": ECR_REGION,
                        },
                    },
                    params={
                        "CONTEXT": str(grader_repo.name),
                        "DOCKERFILE": f"{grader_repo.name}/Dockerfile",
                        "IMAGE_ARG_GRADER_BASE_IMAGE": f"{base_ref}/image.tar",
                    },
                    caches=[Cache(path="cache")],
                    inputs=[
                        Input(name=grader_repo.name),
                        Input(name=grader_base_image.name),
                    ],
                    outputs=[Output(name=Identifier("image"))],
                    run=Command(path="build"),
                ),
            ),
            ensure_ecr_task(config.ecr_repo_name),
            PutStep(
                put=grader_ecr_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": (f"./{grader_repo.name}/.git/describe_ref"),
                },
            ),
        ],
    )

    fragment = PipelineFragment(
        resources=[grader_repo, grader_base_image, grader_ecr_image],
        jobs=[build_job],
    )

    return Pipeline(
        resource_types=fragment.resource_types,
        resources=fragment.resources,
        jobs=fragment.jobs,
    )


# ---------------------------------------------------------------------------
# Configured grader pipelines
# ---------------------------------------------------------------------------

GRADER_PIPELINES: list[GraderPipelineConfig] = [
    GraderPipelineConfig(
        pipeline_name="graders-mit-600x",
        grader_repo_url="git@github.com:mitodl/graders-mit-600x",
        grader_repo_branch="master",
        ecr_repo_name="mitodl/graders-mit-600x",
    ),
    GraderPipelineConfig(
        pipeline_name="graders-mit-686x",
        grader_repo_url="git@github.mit.edu:mitx/graders-mit-686x",
        grader_repo_branch="master",
        ecr_repo_name="mitodl/graders-mit-686x",
        github_private_key="((github_enterprise.private_ssh_key))",
    ),
]


if __name__ == "__main__":
    pipeline_name = sys.argv[1]
    config = next(
        (p for p in GRADER_PIPELINES if p.pipeline_name == pipeline_name), None
    )
    if config is None:
        sys.exit(
            f"Unknown pipeline name {pipeline_name!r}. "
            f"Available: {[p.pipeline_name for p in GRADER_PIPELINES]}"
        )
    pipeline_json = grader_image_pipeline(config).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.write(
        f"\nfly -t <target> set-pipeline"
        f" -p build-{pipeline_name}-image -c definition.json\n"
    )
