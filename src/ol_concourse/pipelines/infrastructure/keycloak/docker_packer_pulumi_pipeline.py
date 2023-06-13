#  noqa: WPS232
import sys

from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS
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
    with open("definition.json", "w") as definition:
        definition.write(build_keycloak_pipeline().json(indent=2))
    sys.stdout.write(build_keycloak_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-keycloak -c definition.json")
    )
