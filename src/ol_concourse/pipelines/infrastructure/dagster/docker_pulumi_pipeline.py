import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
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
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


def build_dagster_docker_pipeline() -> Pipeline:
    data_platform_branch = "main"

    # Define all code location images based on docker-compose.yaml
    code_locations = [
        {"name": "canvas", "module": "canvas.definitions"},
        {"name": "data_loading", "module": "data_loading.definitions"},
        {"name": "data_platform", "module": "data_platform.definitions"},
        {"name": "edxorg", "module": "edxorg.definitions"},
        {"name": "lakehouse", "module": "lakehouse.definitions"},
        {"name": "learning_resources", "module": "learning_resources.definitions"},
        {"name": "legacy_openedx", "module": "legacy_openedx.definitions"},
        {"name": "openedx", "module": "openedx.definitions"},
        {"name": "b2b_organization", "module": "b2b_organization.definitions"},
        {
            "name": "student_risk_probability",
            "module": "student_risk_probability.definitions",
        },
        {"name": "k8s", "module": None},  # dagster-k8s base image
    ]

    # Create git resources for each code location with specific path filters
    code_location_repos = {}
    for location in code_locations:
        name: str = location["name"]  # type: ignore[index]
        # dagster-k8s is the base image, not a code location
        if name == "k8s":
            paths = [
                "dg_deployments/Dockerfile.dagster-k8s",
                "packages/ol-orchestrate-lib/",
            ]
        else:
            paths = [
                f"dg_projects/{name}/",
                "packages/ol-orchestrate-lib/",
            ]
            # Lakehouse also needs the dbt project
            if name == "lakehouse":
                paths.append("src/ol_dbt/")

        code_location_repos[name] = git_repo(
            name=Identifier(f"ol-data-platform-{name}"),
            uri="https://github.com/mitodl/ol-data-platform",
            branch=data_platform_branch,
            paths=paths,
        )

    # Create registry image resources for each code location
    code_location_images = {}
    for location in code_locations:
        location_name: str = location["name"]  # type: ignore[index]
        code_location_images[location_name] = registry_image(
            name=Identifier(f"dagster-{location_name}-image"),
            image_repository=f"mitodl/dagster-{location_name}",
            image_tag=None,
            # While check_every=never, defining tag_regex helps Concourse UI understand
            # resource versions
            tag_regex=r"^[0-9a-f]{4,40}$",  # Git short ref
            sort_by_creation=True,
            ecr_region="us-east-1",
        )

    pulumi_code_branch = "main"
    pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/applications/dagster/",
            "src/bridge/lib/versions.py",
        ],
        branch=pulumi_code_branch,
    )

    # Create build jobs for each code location
    docker_build_jobs = []
    for location in code_locations:
        build_name: str = location["name"]  # type: ignore[index]
        repo = code_location_repos[build_name]
        image = code_location_images[build_name]

        # Determine if this location needs DBT secrets (lakehouse requires it)
        needs_dbt_secrets = build_name == "lakehouse"

        # dagster-k8s uses a different Dockerfile path
        if build_name == "k8s":
            dockerfile_path = f"{repo.name}/dg_deployments/Dockerfile.dagster-k8s"
        else:
            dockerfile_path = f"{repo.name}/dg_projects/{build_name}/Dockerfile"

        build_params = {
            "CONTEXT": repo.name,
            "DOCKERFILE": dockerfile_path,
        }

        if needs_dbt_secrets:
            build_params.update(
                {
                    "BUILDKIT_SECRETTEXT_dbt_trino_username": "((dbt.trino_username))",
                    "BUILDKIT_SECRETTEXT_dbt_trino_password": "((dbt.trino_password))",
                }
            )

        docker_build_job = Job(
            name=f"build-dagster-{build_name}-image",
            plan=[
                GetStep(
                    get=repo.name,
                    trigger=True,
                ),
                container_build_task(
                    inputs=[Input(name=repo.name)],
                    build_parameters=build_params,
                    build_args=[],
                ),
                TaskStep(
                    task=Identifier("collect-tags"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        inputs=[Input(name=repo.name)],
                        outputs=[Output(name="tags")],
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "mitodl/ol-infrastructure",
                                "tag": "latest",
                            },
                        ),
                        run=Command(
                            path="sh",
                            user="root",
                            args=[
                                "-exc",
                                rf"""ls -ltrha;
                                ls -lthra ../;
                                cat ./{repo.name}/.git/describe_ref >> tags/collected_tags;""",  # noqa: E501
                            ],
                        ),
                    ),
                ),
                TaskStep(
                    task=Identifier("ensure-ecr-repository"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={"repository": "amazon/aws-cli", "tag": "latest"},
                        ),
                        params={
                            "REPO_NAME": image.source["repository"],
                            "AWS_PAGER": "cat",
                        },
                        run=Command(
                            path="sh",
                            args=[
                                "-exc",
                                "aws ecr describe-repositories --repository-names ${REPO_NAME} || aws ecr create-repository --repository-name ${REPO_NAME}",  # noqa: E501
                            ],
                        ),
                    ),
                ),
                PutStep(
                    put=image.name,
                    inputs="all",
                    params={
                        "image": "image/image.tar",
                        "additional_tags": "tags/collected_tags",
                    },
                ),
            ],
        )
        docker_build_jobs.append(docker_build_job)

    # Collect env vars from all code location images for Pulumi
    pulumi_env_vars = {}
    for location in code_locations:
        img_name: str = location["name"]  # type: ignore[index]
        image = code_location_images[img_name]
        env_var_name = f"DAGSTER_{img_name.upper()}_IMAGE_TAG"
        pulumi_env_vars[env_var_name] = f"{image.name}/tag"

    # Get dependencies - trigger Pulumi when all images are built
    pulumi_dependencies = [
        GetStep(
            get=image.name,
            trigger=True,
            passed=[f"build-dagster-{loc_name}-image"],
        )
        for loc_name, image in code_location_images.items()
    ]

    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code,
        stack_names=[f"applications.dagster.{stage}" for stage in ("QA", "Production")],
        project_name="ol-infrastructure-dagster-server",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/dagster/"),
        dependencies=pulumi_dependencies,
        env_vars_from_files=pulumi_env_vars,
    )

    combined_fragment = PipelineFragment(
        resource_types=pulumi_fragment.resource_types,
        resources=[
            *code_location_repos.values(),
            *code_location_images.values(),
            pulumi_code,
            *pulumi_fragment.resources,
        ],
        jobs=[*docker_build_jobs, *pulumi_fragment.jobs],
    )

    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=combined_fragment.resources,
        jobs=combined_fragment.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_dagster_docker_pipeline().json(indent=2))
    sys.stdout.write(build_dagster_docker_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-pulumi-dagster -c definition.json")
    )
