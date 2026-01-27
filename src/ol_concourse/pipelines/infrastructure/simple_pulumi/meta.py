"""Meta pipeline for managing simple Pulumi-only deployment pipelines.

This meta pipeline automatically generates and updates individual pipelines for
applications that follow the simple Pulumi-only pattern (no build steps, just
infrastructure deployment across CI/QA/Production).
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


def meta_job(app_name: str) -> Job:
    """Generate a job that creates/updates the pipeline for a specific app.

    Args:
        app_name: The name of the application.

    Returns:
        A Job that generates and sets the pipeline for the app.
    """
    return Job(
        name=Identifier(f"create-{app_name}-pipeline"),
        plan=[
            GetStep(
                get="simple-pulumi-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(f"generate-{app_name}-pipeline-file"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": "mitodl/ol-infrastructure",
                            "tag": "latest",
                        },
                    ),
                    inputs=[
                        Input(name=Identifier("simple-pulumi-pipeline-definitions"))
                    ],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            "../simple-pulumi-pipeline-definitions/src/ol_concourse/pipelines/infrastructure/simple_pulumi/simple_pulumi_pipeline.py",
                            app_name,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(f"pulumi-{app_name}"),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline(app_names: list[str]) -> Pipeline:
    """Generate the meta-pipeline that manages all simple Pulumi app pipelines.

    Args:
        app_names: List of application names to manage.

    Returns:
        A Pipeline that manages all individual app pipelines.
    """
    pipeline_definitions = git_repo(
        name=Identifier("simple-pulumi-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/infrastructure/simple_pulumi/",
            "src/ol_concourse/lib/",
            "src/ol_concourse/pipelines/constants.py",
        ],
    )

    # Create a job for each app to generate its pipeline
    pipeline_jobs = [meta_job(app_name) for app_name in app_names]

    # Add self-updating job for the meta pipeline
    pipeline_jobs.append(
        Job(
            name=Identifier("set-simple-pulumi-meta-pipeline"),
            plan=[
                GetStep(
                    get="simple-pulumi-pipeline-definitions",
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier("generate-simple-pulumi-meta-pipeline-file"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "mitodl/ol-infrastructure",
                                "tag": "latest",
                            },
                        ),
                        inputs=[
                            Input(name=Identifier("simple-pulumi-pipeline-definitions"))
                        ],
                        outputs=[Output(name=Identifier("pipeline"))],
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../simple-pulumi-pipeline-definitions/src/ol_concourse/pipelines/infrastructure/simple_pulumi/meta.py",
                                repr(app_names),
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
        jobs=pipeline_jobs,
    )


if __name__ == "__main__":
    # List of applications using the simple Pulumi-only pattern
    app_names = [
        "airbyte",
        "digital-credentials",
        "fastly-redirector",
        "kubewatch",
        "micromasters",
        "mongodb-atlas",
        "ocw-studio",
        "open-discussions",
        "open-metadata",
        "opensearch",
        "tika",
        "vector-log-proxy",
        "xpro-partner-dns",
    ]

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(meta_pipeline(app_names).model_dump_json(indent=2))
    sys.stdout.write(meta_pipeline(app_names).model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p simple-pulumi-meta -c definition.json")  # noqa: T201
