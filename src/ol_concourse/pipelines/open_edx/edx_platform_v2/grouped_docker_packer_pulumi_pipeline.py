import sys

from bridge.settings.openedx.accessors import (
    fetch_application_version,
    filter_deployments_by_release,
)
from bridge.settings.openedx.types import (
    OpenEdxApplication,
    OpenEdxSupportedRelease,
)

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    GroupConfig,
    Identifier,
    InParallelStep,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_edx_pipeline(release_names: list[str]) -> Pipeline:  # noqa: ARG001
    edx_docker_code = git_repo(
        name=Identifier("ol-infrastructure-docker"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        depth=1,
        paths=[
            "dockerfiles/openedx-edxapp/",
            "src/ol_concourse/pipelines/open_edx/edx_platform_v2",
        ],
    )

    container_fragments = []
    packer_fragments = []
    pulumi_fragments = []
    group_configs = []
    for release_name in releases:
        job_names = []
        for deployment in filter_deployments_by_release(release_name):
            theme_git_resources = []
            theme_get_steps = []

            theme = fetch_application_version(
                release_name, deployment.deployment_name, OpenEdxApplication.theme
            )
            theme_git_resource = git_repo(
                name=Identifier(f"{release_name}-{deployment.deployment_name}-theme"),
                uri=theme.git_origin,
                branch=theme.release_branch,
            )
            theme_git_resources.append(theme_git_resource)
            theme_get_steps.append(GetStep(get=theme_git_resource.name, trigger=True))

            edx_platform_git_resources = []
            edx_platform_get_steps = []
            edx_platform = fetch_application_version(
                release_name, deployment.deployment_name, OpenEdxApplication.edxapp
            )
            edx_platform_git_resource = git_repo(
                name=Identifier(
                    f"{release_name}-{deployment.deployment_name}-edx-platform"
                ),
                uri=edx_platform.git_origin,
                branch=edx_platform.release_branch,
            )
            edx_platform_git_resources.append(edx_platform_git_resource)
            edx_platform_get_steps.append(
                GetStep(get=edx_platform_git_resource.name, trigger=True)
            )

            edx_ami_code = git_repo(
                name=Identifier(
                    f"edxapp-custom-image-{deployment.deployment_name}-{release_name}"
                ),
                uri="https://github.com/mitodl/ol-infrastructure",
                branch="main",
                paths=[
                    "src/bridge/settings/openedx/",
                    "src/bilder/components/",
                    "src/bilder/images/edxapp_v2/deploy.py",
                    "src/bilder/images/edxapp_v2/group_data/",
                    "src/bilder/images/edxapp_v2/files/",
                    "src/bilder/images/edxapp_v2/templates/vector/",
                    f"src/bilder/images/edxapp_v2/templates/edxapp/{deployment.deployment_name}/",
                    "src/bilder/images/edxapp_v2/custom_install.pkr.hcl",
                    f"src/bilder/images/edxapp_v2/packer_vars/{deployment.deployment_name}.pkrvars.hcl",
                    f"src/bilder/images/edxapp_v2/packer_vars/{release_name}.pkrvars.hcl",
                ],
            )

            edx_pulumi_code = git_repo(
                name=Identifier(
                    f"edxapp-ol-infrastructure-pulumi-{deployment.deployment_name}"
                ),
                uri="https://github.com/mitodl/ol-infrastructure",
                branch="main",
                paths=[
                    *PULUMI_WATCHED_PATHS,
                    "src/ol_infrastructure/applications/edxapp/",
                    "src/bridge/secrets/edxapp/",
                    "src/bridge/settings/openedx/",
                ],
            )

            edx_registry_image_resource = registry_image(
                name=Identifier(
                    f"edxapp-{release_name}-{deployment.deployment_name}-image"
                ),
                image_repository="mitodl/edxapp",
                image_tag=f"{release_name}-{deployment.deployment_name}",
                username="((dockerhub.username))",
                password="((dockerhub.password))",  # noqa: S106
            )

            docker_build_inputs = (
                [
                    Input(name=edx_platform_git_resource.name)
                    for edx_platform_git_resource in edx_platform_git_resources
                ]
                + [
                    Input(name=theme_git_resource.name)
                    for theme_git_resource in theme_git_resources
                ]
                + [Input(name=edx_docker_code.name)]
            )

            docker_build_job = Job(
                name=f"build-{release_name}-{deployment.deployment_name}-edxapp-image",
                build_log_retention={"builds": 10},
                max_in_flight=1,
                plan=[
                    InParallelStep(
                        in_parallel=theme_get_steps
                        + edx_platform_get_steps
                        + [GetStep(get=edx_docker_code.name, trigger=True)]
                    ),
                    TaskStep(
                        task=Identifier("collect-code"),
                        config=TaskConfig(
                            platform=Platform.linux,
                            image_resource=AnonymousResource(
                                type="registry-image",
                                source={
                                    "repository": "alpine",
                                    "tag": "3.18.0",
                                },
                            ),
                            inputs=docker_build_inputs,
                            outputs=[
                                Output(name=Identifier("collected_themes")),
                                Output(name=Identifier("edx_platform")),
                                Output(name=Identifier("version")),
                            ],
                            run=Command(
                                path="sh",
                                args=[
                                    "-xc",
                                    f"""cp -r {release_name}-{deployment.deployment_name}-theme collected_themes/{deployment.deployment_name};
                                        cp -rT {release_name}-{deployment.deployment_name}-edx-platform edx_platform;
                                        echo "{release_name}-{deployment.deployment_name}-$(cat edx_platform/.git/short_ref)" > version/tag;""",  # noqa: E501
                                ],
                            ),
                        ),
                    ),
                    container_build_task(
                        inputs=[
                            Input(name=edx_docker_code.name),
                            Input(
                                name=Identifier("collected_themes"),
                                path=f"{edx_docker_code.name}/dockerfiles/openedx-edxapp/collected_themes",
                            ),
                            Input(
                                name=Identifier("edx_platform"),
                                path=f"{edx_docker_code.name}/dockerfiles/openedx-edxapp/edx_platform",
                            ),
                            Input(name=Identifier("version")),
                        ],
                        build_parameters={
                            "CONTEXT": (
                                "ol-infrastructure-docker/dockerfiles/openedx-edxapp"
                            ),
                            "TARGET": "final",
                            "BUILD_ARG_RELEASE_NAME": release_name,
                            "BUILD_ARG_DEPLOYMENT_NAME": deployment.deployment_name,
                        },
                        build_args=[],
                    ),
                    PutStep(
                        put=edx_registry_image_resource.name,
                        params={
                            "image": "image/image.tar",
                            "additional_tags": "version/tag",
                        },
                    ),
                ],
            )

            job_names.append(docker_build_job.name)

            container_fragments.append(
                PipelineFragment(
                    resources=[
                        edx_docker_code,
                        edx_registry_image_resource,
                        edx_ami_code,
                        edx_pulumi_code,
                        *theme_git_resources,
                        *edx_platform_git_resources,
                    ],
                    jobs=[docker_build_job],
                )
            )

            packer_fragments.append(
                packer_jobs(
                    dependencies=[
                        GetStep(
                            get=edx_registry_image_resource.name,
                            trigger=True,
                            passed=[docker_build_job.name],
                        ),
                    ],
                    image_code=edx_ami_code,
                    packer_template_path=(
                        "src/bilder/images/edxapp_v2/custom_install.pkr.hcl"
                    ),
                    node_types=["web", "worker"],
                    env_vars_from_files={
                        "DOCKER_REPO_NAME": (
                            f"{edx_registry_image_resource.name}/repository"
                        ),
                        "DOCKER_IMAGE_DIGEST": (
                            f"{edx_registry_image_resource.name}/digest"
                        ),
                    },
                    packer_vars={"framework": "docker"},
                    extra_packer_params={
                        "only": ["amazon-ebs.edxapp"],
                        "var_files": [
                            f"{edx_ami_code.name}/src/bilder/images/edxapp_v2/packer_vars/{release_name}.pkrvars.hcl",
                            f"{edx_ami_code.name}/src/bilder/images/edxapp_v2/packer_vars/{deployment.deployment_name}.pkrvars.hcl",
                        ],
                    },
                    job_name_suffix=f"{release_name}-{deployment.deployment_name}",
                )
            )
            job_names.append(packer_fragments[-1].jobs[-1].name)
            job_names.append(packer_fragments[-1].jobs[-2].name)

            pulumi_fragments.append(
                pulumi_jobs_chain(
                    edx_pulumi_code,
                    stack_names=[
                        f"applications.edxapp.{deployment.deployment_name}.{stage}"
                        for stage in deployment.envs_by_release(release_name)
                    ],
                    project_name=f"ol-infrastructure-edxapp-application.{deployment.deployment_name}",
                    project_source_path=PULUMI_CODE_PATH.joinpath(
                        "applications/edxapp",
                    ),
                    dependencies=[
                        GetStep(
                            get=packer_fragments[-1].resources[-1].name,
                            trigger=True,
                            passed=[packer_fragments[-1].jobs[-1].name],
                        ),
                    ],
                )
            )
            job_names.extend([job.name for job in pulumi_fragments[-1].jobs])

        combined_fragments = PipelineFragment.combine_fragments(
            *container_fragments, *packer_fragments, *pulumi_fragments
        )
        group_config = GroupConfig(
            name=f"{release_name}",
            jobs=job_names,
        )
        group_configs.append(group_config)
    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
        ],
        jobs=combined_fragments.jobs,
        groups=group_configs,
    )


if __name__ == "__main__":
    releases = [release_name.name for release_name in OpenEdxSupportedRelease]
    pipeline_json = build_edx_pipeline(releases).json(indent=1)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        {
            "\n",
            (
                "fly -t <target> set-pipeline -p docker-packer-pulumi-edxapp-global -c"
                " definition.json"
            ),
        }
    )
