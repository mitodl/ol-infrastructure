# pyright: reportCallIssue=false
"""Meta pipeline for managing simple Pulumi-only deployment pipelines.

This meta pipeline automatically generates and updates individual pipelines for
applications that follow the simple Pulumi-only pattern (no build steps, just
infrastructure deployment across CI/QA/Production).

Two Concourse environments are supported via --env:
  production (default): registers pipelines for the production Concourse instance.
  qa: registers pipelines for the QA Concourse instance (needed for any stack whose
      local.Command resources require direct VPC-level access to QA infrastructure,
      e.g. starrocks-substructure-qa which connects to the QA data-VPC NLB).
"""

import argparse
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
                        source=_OL_INFRA_IMAGE_SOURCE,
                    ),
                    inputs=[
                        Input(name=Identifier("simple-pulumi-pipeline-definitions"))
                    ],
                    outputs=[Output(name=Identifier("pipeline"))],
                    params={"PYTHONPATH": "../simple-pulumi-pipeline-definitions/src"},
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            "../simple-pulumi-pipeline-definitions/src/ol_concourse/pipelines/infrastructure/simple_pulumi/pipeline.py",
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


def meta_pipeline(
    app_names: list[str], extra_args: list[str] | None = None
) -> Pipeline:
    """Generate the meta-pipeline that manages all simple Pulumi app pipelines.

    Args:
        app_names: List of application names to manage.
        extra_args: Extra CLI arguments forwarded to meta.py in the self-update task
            (e.g. ``["--env", "qa"]`` so QA Concourse regenerates its own definition).

    Returns:
        A Pipeline that manages all individual app pipelines.
    """
    pipeline_definitions = git_repo(
        name=Identifier("simple-pulumi-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/infrastructure/simple_pulumi/",
            "pyproject.toml",
            "src/ol_concourse/pipelines/constants.py",
            "src/ol_concourse/pipelines/jobs.py",
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
                            source=_OL_INFRA_IMAGE_SOURCE,
                        ),
                        inputs=[
                            Input(name=Identifier("simple-pulumi-pipeline-definitions"))
                        ],
                        outputs=[Output(name=Identifier("pipeline"))],
                        params={
                            "PYTHONPATH": "../simple-pulumi-pipeline-definitions/src"
                        },
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../simple-pulumi-pipeline-definitions/src/"
                                "ol_concourse/pipelines/infrastructure/simple_pulumi/"
                                "meta.py",
                                *(extra_args or []),
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
    parser = argparse.ArgumentParser(
        description="Generate the simple-pulumi meta pipeline definition."
    )
    parser.add_argument(
        "--env",
        choices=["production", "qa"],
        default="production",
        help=(
            "Target Concourse environment. 'production' (default) generates the "
            "pipeline for the production Concourse instance. 'qa' generates a "
            "pipeline for the QA Concourse instance, which includes apps whose "
            "local.Command resources need direct access to QA VPC infrastructure."
        ),
    )
    cli_args = parser.parse_args()

    production_app_names = [
        "airbyte",
        "aws-ecr",
        "aws-sftp",
        "b2b-partners-storage",
        "celery-monitoring",
        "clickhouse",
        "data_warehouse",
        "digital-credentials",
        "fastly-redirector",
        "jupyterhub-data",
        "mailgun",
        "marimo-data",
        "mongodb-atlas",
        "monitoring",
        "ocw-site",
        "open-discussions",
        "open-metadata",
        "open-metadata-substructure",
        "opensearch",
        "qdrant-cloud",
        "rootly",
        "sentry",
        "starrocks",
        "starrocks-substructure",
        "starburst",
        "tika",
        "toolhive-apps",
        "toolhive-data",
        "toolhive-operator",
        "toolhive-swe",
        "vector-log-proxy",
        "xpro-partner-dns",
    ]

    qa_app_names = [
        "starrocks-substructure-qa",
    ]

    if cli_args.env == "qa":
        app_names = qa_app_names
        extra_args: list[str] | None = ["--env", "qa"]
        fly_target = "qa-inf"
    else:
        app_names = production_app_names
        extra_args = None
        fly_target = "pr-inf"

    pipeline_json = meta_pipeline(app_names, extra_args=extra_args).model_dump_json(
        indent=2
    )
    try:
        with open("definition.json", "w") as definition:  # noqa: PTH123
            definition.write(pipeline_json)
    except OSError as exc:
        msg = "Failed to write simple-pulumi meta pipeline definition.json"
        raise RuntimeError(msg) from exc
    sys.stdout.write(pipeline_json)
    print()  # noqa: T201
    print(  # noqa: T201
        f"fly -t {fly_target} sp -p simple-pulumi-meta -c definition.json"
    )
