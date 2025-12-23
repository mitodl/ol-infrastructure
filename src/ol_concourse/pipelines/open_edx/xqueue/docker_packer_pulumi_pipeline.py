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


def build_xqueue_pipeline(release_name: str):
    openedx_release = OpenEdxSupportedRelease[release_name]
    xqueue_branch = openedx_release.branch
    xqueue_repo = git_repo(
        name=Identifier("openedx-xqueue-code"),
        uri="https://github.com/openedx/xqueue",
        branch=xqueue_branch,
    )

    xqueue_registry_image = registry_image(
        name=Identifier("openedx-xqueue-container"),
        image_repository="mitodl/xqueue",
        image_tag=release_name,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    xqueue_dockerfile_repo = git_repo(
        name=Identifier("xqueue-dockerfile"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "dockerfiles/openedx-xqueue/Dockerfile",
            "dockerfiles/openedx-xqueue/env_config.py",
        ],
    )

    xqueue_packer_code = git_repo(
        name=Identifier("ol-infrastructure-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/xqueue/",
            "src/bilder/components/",
        ],
    )

    xqueue_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/xqueue/"),
            "src/bridge/settings/openedx/",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-xqueue-image"),
        plan=[
            GetStep(get=xqueue_repo.name, trigger=True),
            GetStep(get=xqueue_dockerfile_repo.name, trigger=True),
            container_build_task(
                inputs=[
                    Input(name=xqueue_repo.name),
                    Input(name=xqueue_dockerfile_repo.name),
                ],
                build_parameters={
                    "CONTEXT": (
                        f"{xqueue_dockerfile_repo.name}/dockerfiles/openedx-xqueue"
                    ),
                    "BUILD_ARG_OPENEDX_COMMON_VERSION": xqueue_branch,
                    "BUILD_ARG_PYTHON_VERSION": openedx_release.python_version,
                },
                build_args=[
                    "-t $(cat ./xqueue-release/commit_sha)",
                    f"-t {xqueue_branch}",
                ],
            ),
            PutStep(
                put=xqueue_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqueue_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[xqueue_repo, xqueue_registry_image, xqueue_dockerfile_repo],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_application(release_name, "xqueue"):
        ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=xqueue_registry_image.name,
                    trigger=True,
                    passed=[image_build_job.name],
                )
            ],
            image_code=xqueue_packer_code,
            packer_template_path="src/bilder/images/xqueue/xqueue.pkr.hcl",
            packer_vars={
                "deployment": deployment.deployment_name,
                "openedx_release": release_name,
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(ami_fragment)

        pulumi_fragment = pulumi_jobs_chain(
            xqueue_pulumi_code,
            stack_names=[
                f"applications.xqueue.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-xqueue-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/xqueue/"),
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
            xqueue_pulumi_code,
            xqueue_packer_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_xqueue_pipeline(
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
                f" docker-packer-pulumi-xqueue-{release_name} -c definition.json"
            ),
        )
    )
