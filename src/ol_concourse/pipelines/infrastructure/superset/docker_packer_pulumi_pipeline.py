import sys

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


def build_superset_docker_pipeline() -> Pipeline:
    ol_inf_branch = "deploy_superset"

    upstream_superset_docker_image = registry_image(
        name=Identifier("upstream-superset-docker-image"),
        image_repository="apachesuperset.docker.scarf.sh/apache/superset",
        image_tag="latest",
    )

    docker_code_repo = git_repo(
        Identifier("ol-inf-superset-docker-code"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch=ol_inf_branch,
        paths=["src/ol_superset/"],
    )

    packer_code_repo = git_repo(
        Identifier("ol-inf-superset-packer-code"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch=ol_inf_branch,
        paths=["src/bilder/components/", "src/bilder/images/superset/"],
    )

    pulumi_code_repo = git_repo(
        Identifier("ol-inf-superset-pulumi-code"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch=ol_inf_branch,
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/applications/superset/",
            "src/bridge/secrets/superset",
        ],
    )

    superset_image = registry_image(
        name=Identifier("supserset-image"),
        image_repository="mitodl/superset",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    docker_build_job = Job(
        name="build-superset-image",
        plan=[
            GetStep(get=upstream_superset_docker_image.name, trigger=True),
            GetStep(get=docker_code_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=docker_code_repo.name)],
                build_parameters={
                    "CONTEXT": f"{docker_code_repo.name}/src/ol_superset",
                },
                build_args=[],
            ),
            PutStep(
                put=superset_image.name,
                inputs="all",
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"{docker_code_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    packer_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=superset_image.name,
                trigger=True,
                passed=[docker_build_job.name],
            ),
        ],
        image_code=packer_code_repo,
        packer_template_path="src/bilder/images/superset/superset.pkr.hcl",
        env_vars_from_files={"SUPERSET_IMAGE_SHA": f"{superset_image.name}/digest"},
        extra_packer_params={
            "only": ["amazon-ebs.superset"],
        },
    )

    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code_repo,
        stack_names=[
            f"applications.superset.{stage}" for stage in ("CI", "QA", "Production")
        ],
        project_name="ol-infrastructure-superset-server",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/superset/"),
        dependencies=[
            GetStep(
                get=packer_fragment.resources[-1].name,
                trigger=True,
                passed=[packer_fragment.jobs[-1].name],
            )
        ],
    )

    combined_fragment = PipelineFragment(
        resource_types=packer_fragment.resource_types + pulumi_fragment.resource_types,
        resources=[
            docker_code_repo,
            packer_code_repo,
            pulumi_code_repo,
            superset_image,
            upstream_superset_docker_image,
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
        definition.write(build_superset_docker_pipeline().json(indent=2))
    sys.stdout.write(build_superset_docker_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-superset -c definition.json")
    )
