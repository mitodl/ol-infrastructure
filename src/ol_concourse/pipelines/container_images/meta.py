"""Meta pipeline for managing container image build pipelines.

Each entry in PIPELINE_CONFIGS maps a Concourse pipeline name to the relative
path of its definition script within the ol-infrastructure repository.  The
meta pipeline generates and registers each managed pipeline whenever the
relevant pipeline definition file changes, and keeps itself up-to-date via a
``set_pipeline: self`` job.

Note: ``jupyter_courses.py`` is intentionally excluded — it generates instanced
pipelines (one per course image) using ``--instance-var image_name=<name>``,
which requires a different orchestration approach.

Fly command to bootstrap this meta pipeline:
    python meta.py
    fly -t pr-inf sp -p container-images-meta -c definition.json
"""

import sys

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

_OL_INFRA_IMAGE_SOURCE = {
    "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
    "tag": "latest",
    "aws_region": ECR_REGION,
}

PIPELINE_CONFIGS: list[tuple[str, str]] = [
    (
        "dcind-resource-image",
        "src/ol_concourse/pipelines/container_images/dcind.py",
    ),
    (
        "docker-google-ads-opt-image",
        "src/ol_concourse/pipelines/container_images/google_ads_optimization.py",
    ),
    (
        "docker-hashicorp-release-resource-image",
        "src/ol_concourse/pipelines/container_images/hashicorp_release_resource.py",
    ),
    (
        "docker-mitodl-concourse-npm-resource",
        "src/ol_concourse/pipelines/container_images/mitodl_concourse_npm_resource.py",
    ),
    (
        "ol-python-base-docker",
        "src/ol_concourse/pipelines/container_images/ol_python_base.py",
    ),
    (
        "ol-superset-image",
        "src/ol_concourse/pipelines/container_images/ol_superset.py",
    ),
    (
        "docker-openedx-tubular-image",
        "src/ol_concourse/pipelines/container_images/openedx_tubular.py",
    ),
]


def meta_job(pipeline_name: str, script_path: str) -> Job:
    """Generate a job that creates/updates a single container image pipeline."""
    return Job(
        name=Identifier(f"create-{pipeline_name}"),
        plan=[
            GetStep(
                get="container-image-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(f"generate-{pipeline_name}-file"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source=_OL_INFRA_IMAGE_SOURCE,
                    ),
                    inputs=[
                        Input(name=Identifier("container-image-pipeline-definitions"))
                    ],
                    outputs=[Output(name=Identifier("pipeline"))],
                    params={
                        "PYTHONPATH": "../container-image-pipeline-definitions/src"
                    },
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            f"../container-image-pipeline-definitions/{script_path}",
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(pipeline_name),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    """Generate the meta pipeline managing all container image build pipelines."""
    pipeline_definitions = git_repo(
        name=Identifier("container-image-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/container_images/",
            "pyproject.toml",
            "src/ol_concourse/pipelines/constants.py",
        ],
    )

    jobs = [meta_job(name, path) for name, path in PIPELINE_CONFIGS]

    jobs.append(
        Job(
            name=Identifier("set-container-images-meta-pipeline"),
            plan=[
                GetStep(
                    get="container-image-pipeline-definitions",
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier("generate-container-images-meta-pipeline-file"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source=_OL_INFRA_IMAGE_SOURCE,
                        ),
                        inputs=[
                            Input(
                                name=Identifier("container-image-pipeline-definitions")
                            )
                        ],
                        outputs=[Output(name=Identifier("pipeline"))],
                        params={
                            "PYTHONPATH": "../container-image-pipeline-definitions/src"
                        },
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../container-image-pipeline-definitions/"
                                "src/ol_concourse/pipelines/container_images/meta.py",
                            ],
                        ),
                    ),
                ),
                SetPipelineStep(
                    set_pipeline="self",
                    file="pipeline/definition.json",
                ),
            ],
        )
    )

    return Pipeline(
        resources=[pipeline_definitions],
        jobs=jobs,
    )


if __name__ == "__main__":
    pipeline_json = meta_pipeline().model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p container-images-meta -c definition.json")  # noqa: T201
