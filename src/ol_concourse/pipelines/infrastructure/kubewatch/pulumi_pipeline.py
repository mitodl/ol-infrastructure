"""Concourse pipeline for kubewatch and kubewatch_webhook_handler deployments."""

import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


def build_kubewatch_webhook_handler_pipeline() -> PipelineFragment:
    """Build pipeline for kubewatch webhook handler.

    Builds Docker image first, then pushes to all environment ECR repos
    before deploying with Pulumi.
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

    # ECR image resources for each environment.
    # When ecr_region is set, the registry-image resource constructs the ECR registry
    # URL itself from the AWS account/region, so only the short repo name is used here.
    environments = ("ci", "qa", "production")
    ecr_image_resources = {
        env: registry_image(
            name=Identifier(f"kubewatch-webhook-handler-image-{env}"),
            image_repository=f"kubewatch-webhook-handler-{env}",
            ecr_region="us-east-1",
        )
        for env in environments
    }

    # Build the image once then push to all environment ECR repos
    code_name = webhook_handler_pulumi_code.name
    app_path = "src/ol_infrastructure/applications/kubewatch_webhook_handler"
    docker_build_job = Job(
        name=Identifier("build-kubewatch-webhook-handler-image"),
        plan=[
            GetStep(
                get=code_name,
                trigger=True,
            ),
            container_build_task(
                inputs=[Input(name=code_name)],
                build_parameters={
                    "CONTEXT": f"{code_name}/{app_path}",
                    "DOCKERFILE": f"{code_name}/{app_path}/Dockerfile",
                },
            ),
            *[
                PutStep(
                    put=ecr_image_resources[env].name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"{code_name}/.git/short_ref",
                    },
                )
                for env in environments
            ],
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
        dependencies=[
            GetStep(
                get=ecr_image_resources["ci"].name,
                trigger=True,
                passed=[docker_build_job.name],
            )
        ],
    )

    # Add Docker build job and ECR resources to fragment
    webhook_handler_fragment.jobs.insert(0, docker_build_job)
    for ecr_resource in ecr_image_resources.values():
        webhook_handler_fragment.resources.append(ecr_resource)
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

    # Create dependencies for each kubewatch job to wait for webhook handler.
    # The webhook handler fragment has the build job at index 0, then pulumi
    # jobs for CI (1), QA (2), Production (3).
    custom_dependencies = {}
    for idx, _env in enumerate(("CI", "QA", "Production")):
        webhook_handler_job = webhook_handler_fragment.jobs[idx + 1]
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
