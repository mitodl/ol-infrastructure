"""Generate the meta pipeline which manages the other pipelines."""

from ol_concourse.lib.models.pipeline import (
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Resource,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.libraries.configuration import PIPELINE_CONFIGS

# Resource for the ol-concourse code itself
ol_concourse_repo = git_repo(
    name=Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_concourse/pipelines/libraries/",
        "src/ol_concourse/lib/",
    ],
)

# Resource for a Python image to run the generation script
python_image = Resource(
    name=Identifier("ol-infra-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ol-infrastructure",
        "tag": "latest",
    },
)


def build_meta_job(variant: str) -> Job:
    """Build a job to generate and set a pipeline for a specific variant."""
    pipeline_name = f"{variant}-api-client"
    definition_path = f"generated-pipeline/{pipeline_name}-definition.json"
    return Job(
        name=Identifier(f"set-{pipeline_name}-pipeline"),
        plan=[
            GetStep(get=ol_concourse_repo.name, trigger=True),
            GetStep(get=python_image.name),
            TaskStep(
                task=Identifier("generate-pipeline-definition"),
                image=python_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=ol_concourse_repo.name)],
                    outputs=[Output(name=Identifier("generated-pipeline"))],
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            f"python {ol_concourse_repo.name}/src/ol_concourse/pipelines/libraries/api_clients_pipeline.py --variant {variant} > {definition_path}",  # noqa: E501
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(pipeline_name),
                file=definition_path,
            ),
        ],
    )


def set_self_job() -> Job:
    """Build a job to regenerate and set the meta pipeline itself."""
    definition_path = "generated-pipeline/meta-pipeline-definition.json"
    return Job(
        name=Identifier("set-self-pipeline"),
        plan=[
            GetStep(get=ol_concourse_repo.name, trigger=True),
            GetStep(get=python_image.name),
            TaskStep(
                task=Identifier("generate-meta-pipeline-definition"),
                image=python_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=ol_concourse_repo.name)],
                    outputs=[Output(name=Identifier("generated-pipeline"))],
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            f"python {ol_concourse_repo.name}/src/ol_concourse/pipelines/libraries/meta.py > {definition_path}",  # noqa: E501
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier("ol-api-clients-meta"),
                file=definition_path,
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    """Generate the meta-pipeline for managing API client pipelines."""
    api_client_jobs = [build_meta_job(variant) for variant in PIPELINE_CONFIGS]
    jobs = [set_self_job(), *api_client_jobs]
    return Pipeline(
        resources=[ol_concourse_repo, python_image],
        jobs=jobs,
    )


if __name__ == "__main__":
    import sys

    pipeline = meta_pipeline()
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print("\nfly -t <target> set-pipeline -p self -c <path/to/this/file>")  # noqa: T201
