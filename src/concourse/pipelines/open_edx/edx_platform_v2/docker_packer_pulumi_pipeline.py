#  noqa: WPS232
import sys

from concourse.lib.containers import container_build_task
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
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
from concourse.lib.resources import git_repo, registry_image

from bridge.settings.openedx.accessors import (
    filter_deployments_by_release,
    fetch_application_version,
)
from bridge.settings.openedx.types import (
    OpenEdxApplication,
)


def build_edx_pipeline(release_name: str) -> Pipeline:
    edx_docker_code = git_repo(
        name=Identifier("ol-infrastructure-docker"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="md/edxapp_docker_migration",
        paths=["dockerfiles/openedx-edxapp"],
    )

    # Get the various edx theme repos for each release

    container_fragments = []
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
        theme_get_steps.append(GetStep(get=theme_git_resource.name, trigger=False))

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
            GetStep(get=edx_platform_git_resource.name, trigger=False)
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

        build_inputs = (
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
            plan=[
                InParallelStep(
                    in_parallel=theme_get_steps
                    + edx_platform_get_steps
                    + [GetStep(get=edx_docker_code.name, trigger=False)]
                )
            ]
            + [
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
                        inputs=build_inputs,
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
                        "CONTEXT": "ol-infrastructure-docker/dockerfiles/openedx-edxapp",  # noqa: E501
                        "TARGET": "final",
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

        container_fragments.append(
            PipelineFragment(
                resources=[
                    edx_docker_code,
                    edx_registry_image_resource,
                    *theme_git_resources,
                    *edx_platform_git_resources,
                ],
                jobs=[docker_build_job],
            )
        )

    combined_fragments = PipelineFragment.combine_fragments(*container_fragments)
    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_edx_pipeline(release_name).json(indent=1)
    with open("definition.json", "w") as definition:
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        {
            "\n",
            f"fly -t <target> set-pipeline -p docker-packer-pulumi-edxapp-{release_name} -c definition.json",  # noqa: E501
        }
    )
