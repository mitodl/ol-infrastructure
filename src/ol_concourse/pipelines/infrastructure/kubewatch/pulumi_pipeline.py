"""Concourse pipeline for kubewatch and kubewatch_webhook_handler deployments."""

import sys

from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


def build_kubewatch_webhook_handler_pipeline() -> PipelineFragment:
    """Build pipeline for kubewatch webhook handler.

    Includes Docker build in Pulumi.
    """
    webhook_handler_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-kubewatch-webhook"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath("applications/kubewatch_webhook_handler/")),
        ],
    )

    webhook_handler_fragment = pulumi_jobs_chain(
        pulumi_code=webhook_handler_pulumi_code,
        stack_names=[
            f"applications.kubewatch_webhook_handler.applications.{env}"
            for env in ("CI", "QA", "Production")
        ],
        project_name="ol-infrastructure-kubewatch-webhook-handler",
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "applications/kubewatch_webhook_handler/"
        ),
    )

    webhook_handler_fragment.resources.append(webhook_handler_pulumi_code)
    return webhook_handler_fragment


def build_kubewatch_pipeline() -> PipelineFragment:
    """Build the pipeline for kubewatch Helm chart deployment.

    This depends on the webhook handler being deployed first since kubewatch
    references the webhook handler service URL via StackReference.
    """
    kubewatch_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-kubewatch"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath("applications/kubewatch/")),
        ],
    )

    # Get the webhook handler fragment to establish dependencies
    webhook_handler_fragment = build_kubewatch_webhook_handler_pipeline()
    webhook_handler_code = webhook_handler_fragment.resources[-1]

    # Create dependencies for each kubewatch job to wait for webhook handler
    custom_dependencies = {}
    for idx, _env in enumerate(("CI", "QA", "Production")):
        webhook_handler_job = webhook_handler_fragment.jobs[idx]
        custom_dependencies[idx] = [
            GetStep(
                get=webhook_handler_code.name,
                trigger=True,
                passed=[webhook_handler_job.name],
            )
        ]

    kubewatch_fragment = pulumi_jobs_chain(
        pulumi_code=kubewatch_pulumi_code,
        stack_names=[
            f"applications.kubewatch.applications.{env}"
            for env in ("CI", "QA", "Production")
        ],
        project_name="ol-infrastructure-kubewatch",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/kubewatch/"),
        custom_dependencies=custom_dependencies,
    )

    kubewatch_fragment.resources.append(kubewatch_pulumi_code)
    return kubewatch_fragment


if __name__ == "__main__":
    webhook_handler_fragment = build_kubewatch_webhook_handler_pipeline()
    kubewatch_fragment = build_kubewatch_pipeline()

    pipeline = PipelineFragment.combine_fragments(
        webhook_handler_fragment,
        kubewatch_fragment,
    ).to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p pulumi-kubewatch -c definition.json")
    )
