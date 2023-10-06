import sys

from bridge.settings.openedx.accessors import filter_deployments_by_release
from bridge.settings.openedx.types import DeploymentEnvRelease, OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_forum_pipeline(
    release_name: str, edx_deployments: list[DeploymentEnvRelease]  # noqa: ARG001
):
    forum_branch = OpenEdxSupportedRelease[release_name].branch
    forum_repo = git_repo(
        name=Identifier("openedx-forum-code"),
        uri="https://github.com/openedx/cs_comments_service.git",
        branch=forum_branch,
    )

    forum_registry_image = registry_image(
        name=Identifier("openedx-forum-container"),
        image_repository="mitodl/forum",
        image_tag=release_name,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    forum_dockerfile_repo = git_repo(
        name=Identifier("forum-dockerfile"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=["dockerfiles/openedx-forum/Dockerfile"],
    )

    forum_packer_code = git_repo(
        name=Identifier("ol-infrastructure-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/forum/",
        ],
    )

    forum_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/forum/"),
            "src/bridge/settings/openedx/",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-forum-image"),
        plan=[
            GetStep(get=forum_repo.name, trigger=True),
            GetStep(get=forum_dockerfile_repo.name, trigger=True),
            container_build_task(
                inputs=[
                    Input(name=forum_repo.name),
                    Input(name=forum_dockerfile_repo.name),
                ],
                build_parameters={
                    "CONTEXT": (
                        f"{forum_dockerfile_repo.name}/dockerfiles/openedx-forum"
                    ),
                    "BUILD_ARG_OPENEDX_COMMON_VERSION": forum_branch,
                },
                build_args=[
                    "-t $(cat ./forum-release/commit_sha)",
                    f"-t {forum_branch}",
                ],
            ),
            PutStep(
                put=forum_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{forum_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[forum_repo, forum_registry_image, forum_dockerfile_repo],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name):
        ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=forum_registry_image.name,
                    trigger=True,
                    passed=[image_build_job.name],
                )
            ],
            image_code=forum_packer_code,
            packer_template_path="src/bilder/images/forum/forum.pkr.hcl",
            packer_vars={
                "deployment": deployment.deployment_name,
                "openedx_release": release_name,
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(ami_fragment)

        pulumi_fragment = pulumi_jobs_chain(
            forum_pulumi_code,
            stack_names=[
                f"applications.forum.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-forum-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/forum/"),
            dependencies=[
                GetStep(
                    get=ami_fragment.resources[-1].name,
                    trigger=True,
                    passed=[ami_fragment.jobs[-1].name],
                ),
            ],
        )
        loop_fragments.append(pulumi_fragment)

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment,
        *loop_fragments,
    )

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[*combined_fragments.resources, forum_pulumi_code, forum_packer_code],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_forum_pipeline(
        release_name,
        OpenLearningOpenEdxDeployment,
    ).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        (
            "\n",
            (
                "fly -t <target> set-pipeline -p"
                f" docker-packer-pulumi-forum-{release_name} -c definition.json"
            ),
        )
    )
