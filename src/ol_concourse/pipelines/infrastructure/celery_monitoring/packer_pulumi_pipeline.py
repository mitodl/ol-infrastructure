from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

celery_monitoring_docker_image = registry_image(
    name=Identifier("celery-monitoring-docker-image"),
    image_repository="kodhive/leek",
)

celery_monitoring_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="cpatti_setup_leek",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/celery_monitoring/",
        "src/bridge/lib/versions.py",
        *PACKER_WATCHED_PATHS,
    ],
)

celery_monitoring_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="cpatti_setup_leek",
    paths=[
        *PULUMI_WATCHED_PATHS,
        PULUMI_CODE_PATH.joinpath("applications/celery_monitoring/"),
    ],
)

celery_monitoring_ami_fragment = packer_jobs(
    dependencies=[GetStep(get=celery_monitoring_docker_image.name, trigger=True)],
    image_code=celery_monitoring_image_code,
    packer_template_path="src/bilder/images/celery_monitoring/celery_monitoring.pkr.hcl",
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.celery-monitoring"]},
)

celery_monitoring_pulumi_fragment = pulumi_jobs_chain(
    celery_monitoring_pulumi_code,
    project_name="ol-infrastructure-celery-monitoring-server",
    stack_names=[
        f"applications.celery_monitoring.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/celery_monitoring/"),
    dependencies=[
        GetStep(
            get=celery_monitoring_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[celery_monitoring_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=celery_monitoring_ami_fragment.resource_types
    + celery_monitoring_pulumi_fragment.resource_types,
    resources=celery_monitoring_ami_fragment.resources
    + celery_monitoring_pulumi_fragment.resources,
    jobs=celery_monitoring_ami_fragment.jobs + celery_monitoring_pulumi_fragment.jobs,
)


celery_monitoring_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        celery_monitoring_image_code,
        celery_monitoring_pulumi_code,
        celery_monitoring_docker_image,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(celery_monitoring_pipeline.model_dump_json(indent=2))
    sys.stdout.write(celery_monitoring_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-celery-monitoring -c definition.json")  # noqa: T201
