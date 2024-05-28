import sys

from bridge.settings.openedx.accessors import filter_deployments_by_application
from bridge.settings.openedx.types import OpenEdxSupportedRelease

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


def build_xqwatcher_pipeline(release_name: str):
    xqwatcher_branch = OpenEdxSupportedRelease[release_name].branch
    xqwatcher_repo = git_repo(
        name=Identifier("openedx-xqwatcher-code"),
        uri="https://github.com/openedx/xqwatcher",
        branch=xqwatcher_branch,
    )

    xqwatcher_registry_image = registry_image(
        name=Identifier("openedx-xqwatcher-container"),
        image_repository="mitodl/xqwatcher",
        image_tag=release_name,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    xqwatcher_dockerfile_repo = git_repo(
        name=Identifier("xqwatcher-dockerfile"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=["dockerfiles/openedx-xqwatcher/Dockerfile"],
    )

    xqwatcher_packer_code = git_repo(
        name=Identifier("ol-infrastructure-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/xqwatcher/",
        ],
    )

    xqwatcher_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
            "src/bridge/settings/openedx/",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-xqwatcher-image"),
        plan=[
            GetStep(get=xqwatcher_repo.name, trigger=True),
            GetStep(get=xqwatcher_dockerfile_repo.name, trigger=True),
            container_build_task(
                inputs=[
                    Input(name=xqwatcher_repo.name),
                    Input(name=xqwatcher_dockerfile_repo.name),
                ],
                build_parameters={
                    "CONTEXT": (
                        f"{xqwatcher_dockerfile_repo.name}/dockerfiles/openedx-xqwatcher"
                    ),
                    "BUILD_ARG_OPENEDX_COMMON_VERSION": xqwatcher_branch,
                },
                build_args=[
                    "-t $(cat ./xqwatcher-release/commit_sha)",
                    f"-t {xqwatcher_branch}",
                ],
            ),
            PutStep(
                put=xqwatcher_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[xqwatcher_repo, xqwatcher_registry_image, xqwatcher_dockerfile_repo],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_application(release_name, "xqwatcher"):
        ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=xqwatcher_registry_image.name,
                    trigger=True,
                    passed=[image_build_job.name],
                )
            ],
            image_code=xqwatcher_packer_code,
            packer_template_path="src/bilder/images/xqwatcher/xqwatcher.pkr.hcl",
            packer_vars={
                "deployment": deployment.deployment_name,
                "openedx_release": release_name,
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(ami_fragment)

        pulumi_fragment = pulumi_jobs_chain(
            xqwatcher_pulumi_code,
            stack_names=[
                f"applications.xqwatcher.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-xqwatcher-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
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
        resources=[
            *combined_fragments.resources,
            xqwatcher_pulumi_code,
            xqwatcher_packer_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_xqwatcher_pipeline(
        release_name,
    ).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        (
            "\n",
            (
                "fly -t <target> set-pipeline -p"
                f" docker-packer-pulumi-xqwatcher-{release_name} -c definition.json"
            ),
        )
    )
