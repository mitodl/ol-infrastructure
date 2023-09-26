import sys

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
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
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_keycloak_pipeline() -> Pipeline:
    # When the ol-keycloak-customization repo is ready for it and has doof implemented,
    # this should be split into two resources, one for `release` and another for
    # `release-canidate` branch. Then refs should be updated. See OVS pipeline.
    keycloak_customization_branch = "main"
    keycloak_customization_repo = git_repo(
        Identifier("ol-keycloak-customization"),
        uri="https://github.com/mitodl/ol-keycloak-customization",
        branch=keycloak_customization_branch,
    )

    keycloak_registry_image = registry_image(
        name=Identifier("keycloak-image"),
        image_repository="mitodl/keycloak",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    keycloak_packer_code = git_repo(
        name=Identifier("ol-infrastructure-packer-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/bilder/images/keycloak",
        ],
    )

    keycloak_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/keycloak/"),
        ],
    )

    keycloak_user_migration_plugin_repo = git_repo(
        name=Identifier("keycloak-user-migration-plugin"),
        uri="https://github.com/daniel-frak/keycloak-user-migration",
        branch="main",
    )

    keycloak_metrics_spi_repo = git_repo(
        name=Identifier("keycloak-metrics-spi"),
        uri="https://github.com/aerogear/keycloak-metrics-spi",
        branch="main",
    )

    maven_registry_image = AnonymousResource(
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="maven", tag="3.9.2-eclipse-temurin-17"),
    )

    docker_build_job = Job(
        name="build-keycloak-docker-image",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=keycloak_customization_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=keycloak_customization_repo.name)],
                build_parameters={
                    "CONTEXT": keycloak_customization_repo.name,
                    "DOCKERFILE": (
                        f"{keycloak_customization_repo.name}/Dockerfile.hosted"
                    ),
                    "BUILD_ARGS_FILE": (
                        f"{keycloak_customization_repo.name}/.keycloak_upstream_tag"
                    ),
                    "IMAGE_PLATFORM": "linux/amd64",
                },
                build_args=[],
            ),
            TaskStep(
                task=Identifier("build-user-migration-jar"),
                config=TaskConfig(
                    platform=Platform.linux,
                    inputs=[Input(name=keycloak_user_migration_plugin_repo.name)],
                    outputs=[Output(name="user_migration.jar")],
                    image_resource=maven_registry_image,
                    run=Command(
                        path="sh",
                        user="root",
                        args=[
                            "-exc",
                            f"""cp ./{keycloak_user_migration_plugin_repo.name}/pom.xml /tmp/;
                            cp ./{keycloak_user_migration_plugin_repo.name}/src /tmp/src;
                            cd /tmp;
                            mvn clean package;
                            cp /tmp/target/*.jar /opt/keycloak/providers/user_migration.jar;""",  # noqa: E501
                        ],
                    ),
                ),
            ),
            TaskStep(
                task=Identifier("build-metrics-spi-jar"),
                config=TaskConfig(
                    platform=Platform.linux,
                    inputs=[Input(name=keycloak_metrics_spi_repo.name)],
                    outputs=[Output(name="metrics_spi.jar")],
                    image_resource=maven_registry_image,
                    run=Command(
                        path="sh",
                        user="root",
                        args=[
                            "-exc",
                            f"""cd ./{keycloak_metrics_spi_repo};
                            mvn package;
                            cp *.jar /opt/keycloak/providers/metrics_spi.jar;""",  # noqa: E501
                        ],
                    ),
                ),
            ),
            PutStep(
                put=keycloak_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": (
                        f"./{keycloak_customization_repo.name}/.git/describe_ref"
                    ),
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[keycloak_customization_repo, keycloak_registry_image],
        jobs=[docker_build_job],
    )

    ami_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=keycloak_registry_image.name,
                trigger=True,
                passed=[docker_build_job.name],
            ),
            GetStep(get=keycloak_customization_repo.name, trigger=False),
        ],
        image_code=keycloak_packer_code,
        packer_template_path="src/bilder/images/keycloak/keycloak.pkr.hcl",
        env_vars_from_files={
            "KEYCLOAK_VERSION": f"{keycloak_customization_repo.name}/.git/describe_ref"
        },
        job_name_suffix="keycloak",
    )

    pulumi_fragment = pulumi_jobs_chain(
        keycloak_pulumi_code,
        # Expand stack_names to include QA and Production when the time comes.
        stack_names=[
            f"applications.keycloak.{stage}"
            for stage in [
                "CI",
                "QA",
                "Production",
            ]
        ],
        project_name="ol-infrastructure-keycloak",
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "applications/keycloak",
        ),
        dependencies=[
            GetStep(
                get=ami_fragment.resources[-1].name,
                trigger=True,
                passed=[ami_fragment.jobs[-1].name],
            ),
        ],
        custom_dependencies={
            # Expand this to account for `release` and `release-canidate` branches
            0: [
                GetStep(
                    get=keycloak_customization_repo.name,
                    trigger=True,
                    passed=[ami_fragment.jobs[-1].name],
                ),
            ],
        },
    )

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment,
        ami_fragment,
        pulumi_fragment,
    )
    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            keycloak_packer_code,
            keycloak_pulumi_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_keycloak_pipeline().model_dump_json(indent=2))
    sys.stdout.write(build_keycloak_pipeline().model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-keycloak -c definition.json")
    )
