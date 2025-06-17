import sys
import textwrap

from bridge.lib.versions import KEYCLOAK_VERSION
from ol_concourse.lib.constants import REGISTRY_IMAGE
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
    LoadVarStep,
    Output,
    Platform,
    PutStep,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import (
    git_repo,
    github_release,
    registry_image,
    s3_object,
)
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_keycloak_substructure_pipeline() -> PipelineFragment:
    keycloak_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-substructure"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath("substructure/keycloak/")),
        ],
    )
    substructure_fragment = pulumi_jobs_chain(
        pulumi_code=keycloak_pulumi_code,
        stack_names=[
            f"substructure.keycloak.{env}" for env in ("CI", "QA", "Production")
        ],
        project_name="ol-infrastructure-substructure-keycloak",
        project_source_path=PULUMI_CODE_PATH.joinpath("substructure/keycloak/"),
    )
    substructure_fragment.resources.append(keycloak_pulumi_code)
    return substructure_fragment


def build_keycloak_infrastructure_pipeline() -> PipelineFragment:
    keycloak_upstream_registry_image = registry_image(
        name=Identifier("keycloak-upstream-image"),
        image_repository="quay.io/keycloak/keycloak",
        image_tag=KEYCLOAK_VERSION,
    )
    keycloak_customization_repo = git_repo(
        Identifier("ol-keycloak-customization"),
        uri="https://github.com/mitodl/ol-keycloak",
        branch="main",
    )

    keycloak_registry_image = registry_image(
        name=Identifier("keycloak-image"),
        image_repository="mitodl/keycloak",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    keycloak_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_CODE_PATH.joinpath("applications/keycloak/")),
        ],
    )

    #############################################
    # Keycloak Service Provider Interfaces (SPIs)
    #############################################

    # Repo: https://github.com/jacekkow/keycloak-protocol-cas
    # Use: Provide CAS support through Keycloak for some old apps
    cas_protocol_spi = github_release(
        name=Identifier("cas-protocol-spi"),
        owner="jacekkow",
        repository="keycloak-protocol-cas",
        tag_filter=KEYCLOAK_VERSION,
        order_by="time",
    )

    # Repo: https://github.com/mitodl/keycloakify-starter
    # Use: Keycloakify to enhance theme customization
    keycloakify_spi = github_release(
        name=Identifier("keycloakify-spi"),
        owner="mitodl",
        repository="keycloakify-starter",
    )

    # Repo: https://github.com/mitodl/ol-keycloak
    # Use: OL SPI to customize login process
    ol_spi = github_release(
        name=Identifier("ol-spi"),
        owner="mitodl",
        repository="ol-keycloak",
        check_frequency="1h",
    )

    ol_keycloakify = github_release(
        name=Identifier("ol-keycloakify"),
        owner="mitodl",
        repository="ol-keycloakify",
    )

    scim_plugin = s3_object(
        name="scim_for_kecyloak",
        bucket="ol-eng-artifacts",
        object_regex=r"keycloak/scim-client/scim-for-keycloak-kc-(\d{2}-\d+\.\d+\.\d+)-enterprise.jar",
    )

    #############################################
    image_build_context = Output(name=Identifier("image-build-context"))

    docker_build_job = Job(
        name="build-keycloak-docker-image",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=keycloak_upstream_registry_image.name, trigger=True),
            GetStep(get=cas_protocol_spi.name, trigger=True),
            GetStep(get=keycloak_customization_repo.name, trigger=True),
            GetStep(get=keycloakify_spi.name, trigger=True),
            GetStep(get=ol_keycloakify.name, trigger=True),
            GetStep(get=ol_spi.name, trigger=True),
            GetStep(get=scim_plugin.name, trigger=True),
            TaskStep(
                task=Identifier("collect-artifacts-for-build-context"),
                config=TaskConfig(
                    platform=Platform.linux,
                    outputs=[image_build_context],
                    inputs=[
                        Input(name=keycloak_customization_repo.name),
                        Input(name=cas_protocol_spi.name),
                        Input(name=keycloakify_spi.name),
                        Input(name=ol_keycloakify.name),
                        Input(name=ol_spi.name),
                        Input(name=scim_plugin.name),
                    ],
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="debian", tag="12-slim"),
                    ),
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            textwrap.dedent(
                                f"""\
                        mkdir {image_build_context.name}/plugins/
                        cp -r {keycloak_customization_repo.name}/* {image_build_context.name}/
                        cp -r {cas_protocol_spi.name}/* {image_build_context.name}/plugins/
                        cp -r {keycloakify_spi.name}/* {image_build_context.name}/plugins/
                        cp -r {ol_keycloakify.name}/* {image_build_context.name}/plugins/
                        cp -r {ol_spi.name}/* {image_build_context.name}/plugins/
                        cp -r {scim_plugin.name}/* {image_build_context.name}/plugins/
                        """  # noqa: E501
                            ),
                        ],
                    ),
                ),
            ),
            container_build_task(
                inputs=[
                    Input(name=image_build_context.name),
                    Input(name=keycloak_customization_repo.name),
                ],
                build_parameters={
                    "CONTEXT": image_build_context.name,
                    "DOCKERFILE": (
                        f"{keycloak_customization_repo.name}/Dockerfile.hosted"
                    ),
                    "IMAGE_PLATFORM": "linux/amd64",
                },
                build_args=[],
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
        resources=[
            keycloak_customization_repo,
            keycloak_registry_image,
            cas_protocol_spi,
            keycloakify_spi,
            ol_keycloakify,
            ol_spi,
            scim_plugin,
        ],
        jobs=[docker_build_job],
    )

    pulumi_fragment = pulumi_jobs_chain(
        keycloak_pulumi_code,
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
                get=container_fragment.resources[-1].name,
                trigger=True,
                passed=[container_fragment.jobs[-1].name],
            ),
            GetStep(
                get=keycloak_customization_repo.name,
                trigger=True,
                passed=[container_fragment.jobs[-1].name],
            ),
            GetStep(
                get=keycloak_registry_image.name,
                trigger=True,
                passed=[container_fragment.jobs[-1].name],
            ),
            LoadVarStep(
                load_var="image_digest",
                file=f"{keycloak_registry_image.name}/digest",
                reveal=True,
            ),
        ],
        additional_env_vars={
            "KEYCLOAK_DOCKER_DIGEST": "((.:image_digest))",
        },
    )

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment,
        pulumi_fragment,
    )
    return PipelineFragment(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            keycloak_pulumi_code,
            keycloak_upstream_registry_image,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    pipeline = PipelineFragment.combine_fragments(
        build_keycloak_infrastructure_pipeline(), build_keycloak_substructure_pipeline()
    ).to_pipeline()
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-keycloak -c definition.json")
    )
