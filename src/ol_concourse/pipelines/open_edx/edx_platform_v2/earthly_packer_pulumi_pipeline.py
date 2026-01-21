# ruff: noqa: E501
import sys

from bridge.settings.openedx.accessors import (
    fetch_application_version,
    filter_deployments_by_release,
)
from bridge.settings.openedx.types import (
    OpenEdxApplication,
    OpenEdxSupportedRelease,
)
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
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
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    PutStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, github_release, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_edx_pipeline(release_names: list[str]) -> Pipeline:  # noqa: ARG001
    # This resource will be shared by all releases/deployment combinations
    earthly_git_resource = git_repo(
        name=Identifier("ol-infrastructure-docker"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        check_every="10m",
        depth=1,
        paths=[
            "dockerfiles/openedx-edxapp/",
        ],
    )

    container_fragments = []
    pulumi_fragments = []
    group_configs = []

    for release_name in releases:
        job_names = []
        for deployment in filter_deployments_by_release(release_name):
            deployment_name = deployment.deployment_name

            # Theme related resource setup
            theme_git_resources = []
            theme_get_steps = []
            theme = fetch_application_version(
                release_name, deployment_name, OpenEdxApplication.theme
            )
            theme_git_resource = git_repo(
                name=Identifier(f"{release_name}-{deployment_name}-theme"),
                uri=theme.git_origin,
                branch=theme.release_branch,
                check_every="24h",
            )
            theme_git_resources.append(theme_git_resource)
            theme_get_steps.append(GetStep(get=theme_git_resource.name, trigger=True))

            # edx_platform related resource setup
            edx_platform_git_resources = []
            edx_platform_get_steps = []
            edx_platform = fetch_application_version(
                release_name, deployment_name, OpenEdxApplication.edxapp
            )
            edx_platform_git_resource = git_repo(
                name=Identifier(f"{release_name}-{deployment_name}-edx-platform"),
                uri=edx_platform.git_origin,
                branch=edx_platform.release_branch,
                check_every="24h",
                depth=1,
            )
            edx_platform_git_resources.append(edx_platform_git_resource)
            edx_platform_get_steps.append(
                GetStep(
                    get=edx_platform_git_resource.name,
                    trigger=True,
                    params={"depth": 1},
                )
            )

            node_version = edx_platform.release.node_version

            nodejs_github_release = github_release(
                name=Identifier(f"nodejs-{node_version}-released-version"),
                owner="nodejs",
                repository="node",
                tag_filter=rf"^v({node_version}\.\d+\.\d+)",
                order_by="version",
            )

            # Pulumi code related resource setup
            edx_pulumi_code = git_repo(
                name=Identifier(f"edxapp-ol-infrastructure-pulumi-{deployment_name}"),
                uri="https://github.com/mitodl/ol-infrastructure",
                branch="main",
                depth=1,
                check_every="10m",
                paths=[
                    *PULUMI_WATCHED_PATHS,
                    "src/ol_infrastructure/applications/edxapp/",
                    "src/bridge/secrets/edxapp/",
                    "src/bridge/settings/openedx/",
                ],
            )

            # Docker image related resource setup
            edx_registry_image_resource = registry_image(
                name=Identifier(f"edxapp-{release_name}-{deployment_name}-image"),
                image_repository="mitodl/edxapp",
                image_tag=f"{release_name}-{deployment_name}",
                username="((dockerhub.username))",
                password="((dockerhub.password))",  # noqa: S106
            )

            # Each earthly build requires several inputs, build that list pre-emptively
            earthly_build_job = Job(
                name=f"build-{release_name}-{deployment_name}-edxapp-image",
                build_log_retention={"builds": 10},
                max_in_flight=1,
                plan=[
                    InParallelStep(
                        in_parallel=theme_get_steps
                        + edx_platform_get_steps
                        + [
                            GetStep(
                                get=earthly_git_resource.name,
                                trigger=True,
                                params={"depth": 1},
                            ),
                            GetStep(get=nodejs_github_release.name, trigger=False),
                        ]
                    ),
                    LoadVarStep(
                        load_var="node_version",
                        reveal=True,
                        file=f"{nodejs_github_release.name}/version",
                    ),
                    TaskStep(
                        task=Identifier("build"),
                        privileged=True,
                        config=TaskConfig(
                            platform=Platform.linux,
                            image_resource=AnonymousResource(
                                type="registry-image",
                                source={"repository": "mitodl/dcind", "tag": "0.7.22"},
                            ),
                            # Use some cleverness with path to mount resources within
                            # the earthly git resource so code is where the Earthfile
                            # expects
                            inputs=[
                                Input(name=earthly_git_resource.name),
                                Input(name=edx_platform_git_resource.name),
                                Input(name=theme_git_resource.name),
                            ],
                            outputs=[Output(name=Identifier("artifacts"))],
                            run=Command(
                                path="bash",
                                args=[
                                    "-c",
                                    f"""source /docker-lib.sh;
                                    start_docker;
                                    echo "{release_name}-{deployment_name}-$(cat {edx_platform_git_resource.name}/.git/short_ref)" > artifacts/tag.txt;
                                    cd {earthly_git_resource.name}/dockerfiles/openedx-edxapp;
                                    RELEASE_NAME={release_name};
                                    DEPLOYMENT_NAME={deployment_name};
                                    EDX_PLATFORM_DIR="../../../{edx_platform_git_resource.name}"
                                    THEME_DIR="../../../{theme_git_resource.name}"
                                    PYTHON_VERSION="{edx_platform.runtime_version}"
                                    NODE_VERSION="((.:node_version))"
                                    earthly +all --DEPLOYMENT_NAME="$DEPLOYMENT_NAME" --RELEASE_NAME="$RELEASE_NAME" --EDX_PLATFORM_DIR="$EDX_PLATFORM_DIR" --THEME_DIR="$THEME_DIR" --PYTHON_VERSION="$PYTHON_VERSION" --NODE_VERSION="$NODE_VERSION";
                                    DIGEST=$(docker inspect --format '{{{{.Id}}}}' mitodl/edxapp-$DEPLOYMENT_NAME-$RELEASE_NAME | cut -d ":" -f2);
                                    echo "Saving docker image to tar file in the artifacts directory";
                                    docker save -o ../../../artifacts/image.tar $DIGEST;""",
                                ],
                            ),
                        ),
                    ),
                    PutStep(
                        put=edx_registry_image_resource.name,
                        inputs=[Identifier("artifacts")],
                        params={
                            "image": "artifacts/image.tar",
                            "additional_tags": "artifacts/tag.txt",
                        },
                    ),
                ],
            )

            job_names.append(earthly_build_job.name)

            container_fragments.append(
                PipelineFragment(
                    resources=[
                        earthly_git_resource,
                        edx_registry_image_resource,
                        edx_pulumi_code,
                        nodejs_github_release,
                        *theme_git_resources,
                        *edx_platform_git_resources,
                    ],
                    jobs=[earthly_build_job],
                )
            )

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
                            get=edx_registry_image_resource.name,
                            trigger=False,
                            passed=[earthly_build_job.name],
                            params={"skip_download": True},
                        ),
                    ],
                    additional_env_vars={"OPENEDX_RELEASE": release_name},
                    env_vars_from_files={
                        "EDXAPP_DOCKER_IMAGE_DIGEST": (
                            f"{edx_registry_image_resource.name}/digest"
                        ),
                    },
                )
            )
            job_names.extend([job.name for job in pulumi_fragments[-1].jobs])

        combined_fragments = PipelineFragment.combine_fragments(
            *container_fragments, *pulumi_fragments
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
                "fly -t pr-inf set-pipeline -p docker-pulumi-edxapp-global -c"
                " definition.json"
            ),
        }
    )
