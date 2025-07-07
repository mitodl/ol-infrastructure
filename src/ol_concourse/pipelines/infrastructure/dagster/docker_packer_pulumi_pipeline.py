import sys

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
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


def build_dagster_docker_pipeline() -> Pipeline:
    data_platform_branch = "main"
    data_platform_repo = git_repo(
        Identifier("ol-data-platform"),
        uri="https://github.com/mitodl/ol-data-platform",
        branch=data_platform_branch,
    )

    mono_dagster_image = registry_image(
        name=Identifier("mono-dagster-image"),
        image_repository="mitodl/mono-dagster",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )
    packer_code_branch = "main"
    packer_code = git_repo(
        name=Identifier("ol-infrastructure-packer"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PACKER_WATCHED_PATHS,
            "src/bilder/components/",
            "src/bilder/images/dagster/",
        ],
        branch=packer_code_branch,
    )

    pulumi_code_branch = "main"
    pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[*PULUMI_WATCHED_PATHS, "src/ol_infrastructure/applications/dagster/"],
        branch=pulumi_code_branch,
    )

    docker_build_job = Job(
        name="build-mono-dagster-image",
        plan=[
            GetStep(
                get=data_platform_repo.name,
                trigger=True,
                params={"skip_download": True},
            ),
            container_build_task(
                inputs=[Input(name=data_platform_repo.name)],
                build_parameters={
                    "CONTEXT": data_platform_repo.name,
                    "DOCKERFILE": f"{data_platform_repo.name}/dockerfiles/orchestrate/Dockerfile.global",  # noqa: E501
                    "BUILDKIT_SECRETTEXT_dbt_trino_username": "((dbt.trino_username))",
                    "BUILDKIT_SECRETTEXT_dbt_trino_password": "((dbt.trino_password))",
                },
                build_args=[],
            ),
            TaskStep(
                task=Identifier("collect-tags"),
                config=TaskConfig(
                    platform=Platform.linux,
                    inputs=[Input(name=data_platform_repo.name)],
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
                            f"""ls -ltrha;
                            ls -lthra ../;
                            egrep -A1 "^name = \\"dagster\\"$" {data_platform_repo.name}/poetry.lock | tail -n 1 | cut -d'"' -f2 >> tags/collected_tags;
                            echo " " >> tags/collected_tags;
                            cat ./{data_platform_repo.name}/.git/describe_ref >> tags/collected_tags;""",  # noqa: E501
                        ],
                    ),
                ),
            ),
            PutStep(
                put=mono_dagster_image.name,
                inputs="all",
                params={
                    "image": "image/image.tar",
                    "additional_tags": "tags/collected_tags",
                },
            ),
        ],
    )

    packer_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=mono_dagster_image.name,
                trigger=True,
                passed=[docker_build_job.name],
            )
        ],
        image_code=packer_code,
        packer_template_path="src/bilder/images/dagster/dagster.pkr.hcl",
        env_vars_from_files={
            "DOCKER_REPO_NAME": f"{mono_dagster_image.name}/repository",
            "DOCKER_IMAGE_DIGEST": f"{mono_dagster_image.name}/digest",
        },
        extra_packer_params={
            "only": ["amazon-ebs.dagster"],
        },
    )

    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code,
        stack_names=[f"applications.dagster.{stage}" for stage in ("QA", "Production")],
        project_name="ol-infrastructure-dagster-server",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/dagster/"),
        dependencies=[
            GetStep(
                get=packer_fragment.resources[-1].name,
                trigger=True,
                passed=[packer_fragment.jobs[-1].name],
            ),
        ],
    )

    combined_fragment = PipelineFragment(
        resource_types=packer_fragment.resource_types + pulumi_fragment.resource_types,
        resources=[
            data_platform_repo,
            mono_dagster_image,
            packer_code,
            pulumi_code,
            *packer_fragment.resources,
            *pulumi_fragment.resources,
        ],
        jobs=[docker_build_job, *packer_fragment.jobs, *pulumi_fragment.jobs],
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
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-dagster -c definition.json")
    )
