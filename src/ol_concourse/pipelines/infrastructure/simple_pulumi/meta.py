"""Meta pipeline for managing simple Pulumi-only deployment pipelines.

This meta pipeline automatically generates and updates individual pipelines for
applications that follow the simple Pulumi-only pattern (no build steps, just
infrastructure deployment across CI/QA/Production).
"""

import json
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
                            path="sh",
                            dir="pipeline",
                            user="root",
                            args=[
                                "-c",
                                (
                                    "python ../simple-pulumi-pipeline-definitions/src/"
                                    "ol_concourse/pipelines/infrastructure/simple_pulumi/"
                                    f"meta.py '{json.dumps(app_names)}'"
                                ),
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
    # Check if app_names was passed as a command line argument (for self-update)
    if len(sys.argv) > 1:
        # Parse the app_names list from the command line argument
        app_names = json.loads(sys.argv[1])
    else:
        # Use the default list of applications
        app_names = [
            "airbyte",
            "bootcamps",
            "celery-monitoring",
            "data_warehouse",
            "digital-credentials",
            "fastly-redirector",
            "micromasters",
            "mongodb-atlas",
            "ocw-studio",
            "open-discussions",
            "open-metadata",
            "opensearch",
            "starrocks",
            "tika",
            "vector-log-proxy",
            "xpro",
            "xpro-partner-dns",
        ]

    pipeline_json = meta_pipeline(app_names).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p simple-pulumi-meta -c definition.json")  # noqa: T201
