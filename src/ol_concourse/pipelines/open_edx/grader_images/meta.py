"""
Meta pipeline for grader image build pipelines.

Creates and maintains two types of pipelines:
  1. A base image pipeline (build-grader-base-image) that builds
     grader_support/Dockerfile.base from the xqueue-watcher repo and pushes
     to both DockerHub and ECR.
  2. One build pipeline per entry in GRADER_PIPELINES that builds and pushes
     a course-specific grader image to private ECR.

This meta pipeline is self-updating: the "create-grader-images-meta-pipeline"
job re-sets itself whenever the pipeline code in ol-infrastructure changes.

Usage:
    fly -t <target> set-pipeline -p grader-images-meta -c definition.json
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
from ol_concourse.pipelines.open_edx.grader_images.build_pipeline import (
    GRADER_PIPELINES,
)

_PIPELINE_CODE_PATHS = [
    "src/ol_concourse/lib/",
    "src/ol_concourse/pipelines/open_edx/grader_images/",
]

pipeline_code = git_repo(
    name=Identifier("grader-images-pipeline-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="feat/xqwatcher-kubernetes-migration",
    paths=_PIPELINE_CODE_PATHS,
)

_OL_INFRA_IMAGE = AnonymousResource(
    type="registry-image",
    source={
        "repository": "mitodl/ol-infrastructure",
        "tag": "latest",
    },
)


def _generate_pipeline_task(
    task_name: str, script_path: str, script_args: list[str]
) -> TaskStep:
    """Return a TaskStep that runs a pipeline-definition script.

    The script writes ``definition.json`` to the ``pipeline`` output directory,
    which the subsequent ``SetPipelineStep`` reads.
    """
    return TaskStep(
        task=Identifier(task_name),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=_OL_INFRA_IMAGE,
            inputs=[Input(name=pipeline_code.name)],
            outputs=[Output(name=Identifier("pipeline"))],
            run=Command(
                path="python",
                dir="pipeline",
                user="root",
                args=[f"../{pipeline_code.name}/{script_path}", *script_args],
            ),
        ),
    )


def _build_base_image_meta_job() -> Job:
    """Job that creates/updates the grader base image build pipeline."""
    return Job(
        name=Identifier("create-grader-base-image-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            _generate_pipeline_task(
                task_name="generate-base-image-pipeline-definition",
                script_path=(
                    "src/ol_concourse/pipelines/open_edx/"
                    "grader_images/base_image_pipeline.py"
                ),
                script_args=[],
            ),
            SetPipelineStep(
                team="infrastructure",
                set_pipeline=Identifier("build-grader-base-image"),
                file="pipeline/definition.json",
            ),
        ],
    )


def _build_grader_meta_job(pipeline_name: str) -> Job:
    """Job that creates/updates the build pipeline for one grader repo."""
    return Job(
        name=Identifier(f"create-{pipeline_name}-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            _generate_pipeline_task(
                task_name=f"generate-{pipeline_name}-pipeline-definition",
                script_path=(
                    "src/ol_concourse/pipelines/open_edx/"
                    "grader_images/build_pipeline.py"
                ),
                script_args=[pipeline_name],
            ),
            SetPipelineStep(
                team="infrastructure",
                set_pipeline=Identifier(f"build-{pipeline_name}-image"),
                file="pipeline/definition.json",
            ),
        ],
    )


def _build_self_update_job() -> Job:
    """Job that keeps the meta pipeline itself in sync with the repo."""
    return Job(
        name=Identifier("create-grader-images-meta-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            _generate_pipeline_task(
                task_name="generate-meta-pipeline-definition",
                script_path=(
                    "src/ol_concourse/pipelines/open_edx/grader_images/meta.py"
                ),
                script_args=[],
            ),
            SetPipelineStep(
                team="main",
                set_pipeline="self",
                file="pipeline/definition.json",
            ),
        ],
    )


meta_jobs = [
    _build_self_update_job(),
    _build_base_image_meta_job(),
    *[_build_grader_meta_job(config.pipeline_name) for config in GRADER_PIPELINES],
]

meta_pipeline = Pipeline(resources=[pipeline_code], jobs=meta_jobs)


if __name__ == "__main__":
    pipeline_json = meta_pipeline.model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p grader-images-meta -c definition.json\n"
    )
