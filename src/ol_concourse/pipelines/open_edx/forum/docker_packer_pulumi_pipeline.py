import sys
from urllib.parse import urlparse

from bridge.settings.openedx.accessors import (
    fetch_application_version,
    filter_deployments_by_release,
)
from bridge.settings.openedx.types import DeploymentEnvRelease
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
    release_name: str, edx_deployments: list[DeploymentEnvRelease]
):
    # Different deployments might have different overrides for origin or branch that
    # need to be applied. This will result in separate containers needing to be built
    # and deployed.
    app_versions = {}
    forum_tag_template = "{release_name}-{repo_owner}-{branch}"

    def repo_owner_fn(origin):
        return urlparse(origin).path.strip("/").split("/")[0]

    for deployment in edx_deployments:
        app_versions[deployment.deployment_name] = fetch_application_version(
            release_name, deployment.deployment_name, "forum"
        )
    origin_branches = {
        (app_version.git_origin, app_version.release_branch)
        for app_version in app_versions.values()
    }

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
            "src/bilder/components/",
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

    container_fragments = []
    for origin, branch in origin_branches:
        repo_owner = repo_owner_fn(origin)
        forum_tag = forum_tag_template.format(
            release_name=release_name,
            repo_owner=repo_owner,
            branch=branch.replace("/", "_"),
        )

        forum_repo = git_repo(
            name=Identifier(f"openedx-forum-code-{repo_owner}"),
            uri=origin,
            branch=branch,
        )

        forum_registry_image = registry_image(
            name=Identifier(f"openedx-forum-container-{repo_owner}"),
            image_repository="mitodl/forum",
            image_tag=release_name,
            username="((dockerhub.username))",
            password="((dockerhub.password))",  # noqa: S106
        )

        image_build_job = Job(
            name=Identifier(f"build-forum-image-{repo_owner}"),
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
                        "BUILD_ARG_DOCKER_IMAGE_TAG": "3.3-slim-bullseye"
                        if release_name != "quince"
                        else "3.0-slim-bullseye",
                        "BUILD_ARG_GEMFILE_FILE": "Gemfile"
                        if release_name in ["master", "redwood"]
                        else "Gemfile3",
                        "BUILD_ARG_OPENEDX_COMMON_VERSION": branch,
                        "BUILD_ARG_OPENEDX_FORUM_REPOSITORY": origin,
                    },
                    build_args=[
                        "-t $(cat ./forum-release/commit_sha)",
                        f"-t {forum_tag}",
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

        container_fragments.append(
            PipelineFragment(
                resources=[forum_repo, forum_registry_image, forum_dockerfile_repo],
                jobs=[image_build_job],
            )
        )

    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name):
        repo_owner = repo_owner_fn(app_versions[deployment.deployment_name].git_origin)
        ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=Identifier(f"openedx-forum-container-{repo_owner}"),
                    trigger=True,
                    passed=[f"build-forum-image-{repo_owner}"],
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
        *container_fragments,
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
        filter_deployments_by_release(release_name),
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
