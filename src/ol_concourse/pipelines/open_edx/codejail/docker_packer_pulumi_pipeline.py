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


def build_codejail_pipeline(
    release_name: str,
    edx_deployments: list[DeploymentEnvRelease],  # noqa: ARG001
):
    openedx_release = OpenEdxSupportedRelease[release_name]
    openedx_branch = openedx_release.branch
    codejail_repo = git_repo(
        name=Identifier("openedx-codejail-code"),
        uri="https://github.com/eduNEXT/codejailservice",
        branch="main",
    )

    codejail_registry_image = registry_image(
        name=Identifier("openedx-codejail-container"),
        image_repository="mitodl/codejail",
        image_tag=release_name,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    codejail_dockerfile_repo = git_repo(
        name=Identifier("codejail-dockerfile"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=["dockerfiles/openedx-codejail/"],
    )

    codejail_packer_code = git_repo(
        name=Identifier("ol-infrastructure-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/codejail/",
        ],
    )

    codejail_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/codejail/"),
            "src/bridge/settings/openedx",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-codejail-image"),
        plan=[
            GetStep(get=codejail_repo.name, trigger=True),
            GetStep(get=codejail_dockerfile_repo.name, trigger=True),
            container_build_task(
                inputs=[
                    Input(name=codejail_repo.name),
                    Input(name=codejail_dockerfile_repo.name),
                ],
                build_parameters={
                    "CONTEXT": (
                        f"{codejail_dockerfile_repo.name}/dockerfiles/openedx-codejail/"
                    ),
                    "BUILD_ARG_OPENEDX_BRANCH": openedx_branch,
                    "BUILD_ARG_PYTHON_VERSION": openedx_release.python_version,
                    "BUILD_ARG_OPENEDX_RELEASE": release_name
                    if release_name != "master"
                    else "",
                },
                build_args=[
                    "-t $(cat ./codejail-release/commit_sha)",
                    f"-t {openedx_branch}",
                ],
            ),
            PutStep(
                put=codejail_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{codejail_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[codejail_repo, codejail_registry_image, codejail_dockerfile_repo],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name):
        ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=codejail_registry_image.name,
                    trigger=True,
                    passed=[image_build_job.name],
                )
            ],
            image_code=codejail_packer_code,
            packer_template_path="src/bilder/images/codejail/codejail.pkr.hcl",
            packer_vars={
                "deployment": deployment.deployment_name,
                "openedx_release": release_name,
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(ami_fragment)

        pulumi_fragment = pulumi_jobs_chain(
            codejail_pulumi_code,
            stack_names=[
                f"applications.codejail.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-codejail-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/codejail/"),
            dependencies=[
                GetStep(
                    get=codejail_registry_image.name,
                    trigger=True,
                    passed=[image_build_job.name],
                    params={"skip_download": True},
                ),
                GetStep(
                    get=ami_fragment.resources[-1].name,
                    trigger=True,
                    passed=[ami_fragment.jobs[-1].name],
                ),
            ],
            env_vars_from_files={
                "CODEJAIL_DOCKER_IMAGE_DIGEST": (
                    f"{codejail_registry_image.name}/digest"
                ),
            },
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
            codejail_pulumi_code,
            codejail_packer_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_codejail_pipeline(
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
                f" docker-packer-pulumi-codejail-{release_name} -c definition.json"
            ),
        )
    )
