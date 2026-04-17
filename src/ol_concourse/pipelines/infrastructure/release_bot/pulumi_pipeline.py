"""Concourse pipeline for release-bot deployment."""

import sys

from ol_concourse.lib.containers import container_build_task
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
from ol_concourse.pipelines.jobs import pulumi_jobs_chain


def build_release_bot_pipeline() -> PipelineFragment:
    """Build pipeline for the Slack release bot.

    Builds Docker image first, then deploys with Pulumi.
    """
    release_bot_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-release-bot"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath("applications/release_bot/")),
        ],
    )

    ecr_image_resource = registry_image(
        name=Identifier("release-bot-image"),
        image_repository="release-bot-ci",
        ecr_region="us-east-1",
    )

    code_name = release_bot_pulumi_code.name
    app_path = "src/ol_infrastructure/applications/release_bot"
    docker_build_job = Job(
        name=Identifier("build-release-bot-image-ci"),
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
            PutStep(
                put=ecr_image_resource.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"{code_name}/.git/short_ref",
                },
            ),
        ],
    )

    release_bot_fragment = pulumi_jobs_chain(
        pulumi_code=release_bot_pulumi_code,
        stack_names=[
            f"applications.release_bot.applications.{env}"
            for env in ("CI", "QA", "Production")
        ],
        project_name="ol-infrastructure-release-bot",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/release_bot/"),
        dependencies=[
            GetStep(
                get=ecr_image_resource.name,
                trigger=True,
                passed=[docker_build_job.name],
            )
        ],
    )

    release_bot_fragment.jobs.insert(0, docker_build_job)
    release_bot_fragment.resources.append(ecr_image_resource)
    release_bot_fragment.resources.append(release_bot_pulumi_code)
    return release_bot_fragment


if __name__ == "__main__":
    fragment = build_release_bot_pipeline()
    pipeline = fragment.to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p pulumi-release-bot -c definition.json")
    )
