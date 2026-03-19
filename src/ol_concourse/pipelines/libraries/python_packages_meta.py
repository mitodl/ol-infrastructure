"""Generate the meta pipeline that manages Python monorepo publish pipelines."""

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

OL_INFRASTRUCTURE_REPO = git_repo(
    name=Identifier("python-package-pipeline-definitions"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_concourse/lib/",
        "src/ol_concourse/pipelines/libraries/",
        "src/ol_concourse/pipelines/open_edx/open_edx_plugins/",
    ],
)

OPEN_EDX_PLUGINS_REPO = git_repo(
    name=Identifier("open-edx-plugins-source"),
    uri="https://github.com/mitodl/open-edx-plugins",
    branch="main",
    paths=["src/", "pyproject.toml", "uv.lock"],
)

OL_DJANGO_REPO = git_repo(
    name=Identifier("ol-django-source"),
    uri="https://github.com/mitodl/ol-django",
    branch="main",
    paths=["src/", "pyproject.toml", "uv.lock"],
)

PIPELINE_IMAGE = AnonymousResource(
    type="registry-image",
    source={
        "repository": "mitodl/ol-infrastructure",
        "tag": "latest",
    },
)


def set_child_pipeline_job(
    *,
    job_name: str,
    pipeline_name: str,
    generator_script: str,
    source_resource_name: str,
) -> Job:
    """Build a job that generates and sets a child pipeline."""

    return Job(
        name=Identifier(job_name),
        plan=[
            GetStep(get=OL_INFRASTRUCTURE_REPO.name, trigger=True),
            GetStep(get=Identifier(source_resource_name), trigger=True),
            TaskStep(
                task=Identifier(f"generate-{pipeline_name}-definition"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=PIPELINE_IMAGE,
                    inputs=[
                        Input(name=OL_INFRASTRUCTURE_REPO.name),
                        Input(name=Identifier(source_resource_name)),
                    ],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="sh",
                        dir="pipeline",
                        user="root",
                        args=[
                            "-exc",
                            (
                                "python "
                                f"../{OL_INFRASTRUCTURE_REPO.name}/{generator_script} "
                                f"../{source_resource_name}"
                            ),
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


def self_update_job() -> Job:
    """Build the job that regenerates this meta pipeline."""

    return Job(
        name=Identifier("set-python-packages-meta-pipeline"),
        plan=[
            GetStep(get=OL_INFRASTRUCTURE_REPO.name, trigger=True),
            TaskStep(
                task=Identifier("generate-python-packages-meta-definition"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=PIPELINE_IMAGE,
                    inputs=[Input(name=OL_INFRASTRUCTURE_REPO.name)],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            (
                                "../python-package-pipeline-definitions/src/"
                                "ol_concourse/pipelines/libraries/python_packages_meta.py"
                            )
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


def meta_pipeline() -> Pipeline:
    """Generate the meta pipeline definition."""

    return Pipeline(
        resources=[
            OL_INFRASTRUCTURE_REPO,
            OPEN_EDX_PLUGINS_REPO,
            OL_DJANGO_REPO,
        ],
        jobs=[
            self_update_job(),
            set_child_pipeline_job(
                job_name="set-open-edx-plugins-pipeline",
                pipeline_name="publish-open-edx-plugins-pypi",
                generator_script=(
                    "src/ol_concourse/pipelines/open_edx/open_edx_plugins/"
                    "build_publish_plugins.py"
                ),
                source_resource_name=str(OPEN_EDX_PLUGINS_REPO.name),
            ),
            set_child_pipeline_job(
                job_name="set-ol-django-pipeline",
                pipeline_name="publish-ol-django-pypi",
                generator_script=("src/ol_concourse/pipelines/libraries/ol_django.py"),
                source_resource_name=str(OL_DJANGO_REPO.name),
            ),
        ],
    )


if __name__ == "__main__":
    pipeline = meta_pipeline()
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stderr.write("\n")
    sys.stderr.write(
        "fly -t pr-main sp -p publish-python-packages-meta -c definition.json\n"
    )
