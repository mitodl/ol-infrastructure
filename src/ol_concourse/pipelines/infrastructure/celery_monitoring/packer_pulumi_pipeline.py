import sys

from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Pipeline,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_celery_monitoring_pipeline() -> Pipeline:
    celery_monitoring_image = registry_image(
        name=Identifier("upstream-leek-image"), image_repository="kodhive/leek"
    )
    packer_code_branch = "cpatti_setup_leek"
    packer_code = git_repo(
        name=Identifier("ol-infrastructure-packer"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=["src/bilder/components/", "src/bilder/images/celery_monitoring/"],
        branch=packer_code_branch,
    )

    pulumi_code_branch = "cpatti_setup_leek"
    pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/applications/celery_monitoring/",
        ],
        branch=pulumi_code_branch,
    )

    packer_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=celery_monitoring_image.name,
                trigger=True,
            )
        ],
        image_code=packer_code,
        packer_template_path="src/bilder/images/celery_monitoring/celery_monitoring.pkr.hcl",
        env_vars_from_files={
            "DOCKER_REPO_NAME": f"{celery_monitoring_image.name}/repository",
            "DOCKER_IMAGE_DIGEST": f"{celery_monitoring_image.name}/digest",
        },
        extra_packer_params={
            "only": ["amazon-ebs.celery_monitoring"],
        },
    )

    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code,
        stack_names=[
            f"applications.celery_monitoring.{stage}" for stage in ("QA", "Production")
        ],
        project_name="ol-infrastructure-celery_monitoring-server",
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "applications/celery_monitoring/"
        ),
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
            celery_monitoring_image,
            packer_code,
            pulumi_code,
            *packer_fragment.resources,
            *pulumi_fragment.resources,
        ],
        jobs=[*packer_fragment.jobs, *pulumi_fragment.jobs],
    )

    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=combined_fragment.resources,
        jobs=combined_fragment.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_celery_monitoring_pipeline().json(indent=2))
    sys.stdout.write(build_celery_monitoring_pipeline().json(indent=2))
    sys.stdout.writelines(
        (
            "\n",
            "fly -t pr-inf sp -p packer-pulumi-celery-monitoring -c definition.json",
        )
    )
