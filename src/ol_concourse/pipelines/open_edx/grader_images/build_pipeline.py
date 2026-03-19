"""
Reusable pipeline builder for course-specific grader images.

Each grader repository (e.g. graders-mit-600x) extends the xqueue-watcher
grader base image with course-specific grader scripts and dependencies.
This module provides a ``GraderPipelineConfig`` dataclass and a
``grader_image_pipeline()`` factory that returns a ``Pipeline`` for building
and pushing that course image to a private ECR repository.

Triggers:
  - New commit to the grader repo (grader scripts or Dockerfile changed).
  - New digest of the grader base image in ECR (base image rebuilt / security
    patch applied).

The base image digest is resolved at build time by reading the ``repository``
and ``digest`` files that Concourse's ``registry-image`` resource writes for
every fetched image.  The resolved ``repo@sha256:…`` reference is injected
into the Docker build as ``GRADER_BASE_IMAGE`` via a shell wrapper around the
``oci-build-task``'s ``build`` script so that the build layer cache is
correctly invalidated and the published image records the exact base used.
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
        grader_base_ecr_repo: ECR repository name (without the registry host)
            for the grader base image used as the build trigger.  Defaults to
            the standard MIT OL base image repo name.
        github_private_key: Vault path for the SSH private key used to clone
            the (private) grader repository.
        aws_account_id: AWS account ID that hosts the ECR registry.
        aws_region: AWS region for ECR authentication.
    """

    pipeline_name: str
    grader_repo_url: str
    grader_repo_branch: str
    ecr_repo_name: str
    grader_base_ecr_repo: str = "mitodl/xqueue-watcher-grader-base"
    github_private_key: str = "((github.ssh_private_key))"
    aws_account_id: str = _AWS_ACCOUNT_ID
    aws_region: str = _AWS_REGION


def grader_image_pipeline(config: GraderPipelineConfig) -> Pipeline:
    """Return a Pipeline that builds and pushes a course-specific grader image.

    The pipeline contains a single build job that:
      1. Watches the grader repo for new commits (trigger).
      2. Watches the grader base image in ECR for updates (trigger).
      3. Builds the Dockerfile in the root of the grader repo.  A shell
         wrapper reads the ``repository`` and ``digest`` files written by the
         ``registry-image`` resource and sets ``BUILD_ARG_GRADER_BASE_IMAGE``
         to the immutable ``repo@sha256:…`` reference before invoking the
         ``oci-build-task``'s ``build`` script.
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

    # Grader base image in ECR — used as a build trigger so that rebuilding
    # the base image automatically causes this pipeline to run.
    grader_base_image = registry_image(
        name=Identifier("grader-base-image"),
        image_repository=config.grader_base_ecr_repo,
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

    # The registry-image resource writes `repository` and `digest` files into
    # the fetched directory.  We read them inside the task via a shell wrapper
    # that sets BUILD_ARG_GRADER_BASE_IMAGE=repo@sha256:… before exec-ing the
    # oci-build-task `build` script.  This pins the base image to the exact
    # digest that triggered the pipeline run, ensuring reproducibility and
    # correct Docker layer-cache invalidation.
    #
    # Note: oci-build-task `params` are env vars injected verbatim — shell
    # expressions like $(cat …) are NOT evaluated there.  The `run.args` shell
    # wrapper is the only way to dynamically set a BUILD_ARG from a file.
    base_ref = grader_base_image.name
    build_job = Job(
        name=Identifier(f"build-{config.pipeline_name}-image"),
        plan=[
            GetStep(get=grader_repo.name, trigger=True),
            GetStep(get=grader_base_image.name, trigger=True),
            TaskStep(
                task=Identifier("build-container-image"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource={
                        "type": "registry-image",
                        "source": {"repository": "concourse/oci-build-task"},
                    },
                    params={
                        "CONTEXT": str(grader_repo.name),
                        "DOCKERFILE": f"{grader_repo.name}/Dockerfile",
                    },
                    caches=[Cache(path="cache")],
                    inputs=[
                        Input(name=grader_repo.name),
                        Input(name=grader_base_image.name),
                    ],
                    outputs=[Output(name=Identifier("image"))],
                    # Read the base image digest file at runtime and export it
                    # as BUILD_ARG_GRADER_BASE_IMAGE before running `build`.
                    run=Command(
                        path="sh",
                        args=[
                            "-euc",
                            (
                                f"export BUILD_ARG_GRADER_BASE_IMAGE="
                                f'"$(cat {base_ref}/repository)'
                                f'@$(cat {base_ref}/digest)"'
                                " && exec build"
                            ),
                        ],
                    ),
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
        grader_repo_branch="feat/containerized-grader",
        ecr_repo_name="mitodl/graders-mit-600x",
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
