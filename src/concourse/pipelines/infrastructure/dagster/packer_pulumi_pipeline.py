from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resources import git_repo, registry_image

dagster_container_resources = [
    registry_image(
        name=Identifier("dagit-container"),
        image_repository="mitodl/data-platform-dagit",
    ),
    registry_image(
        name=Identifier("dagster-daemon-container"),
        image_repository="mitodl/data-platform-dagster-daemon",
    ),
    registry_image(
        name=Identifier("edx-pipeline-container"),
        image_repository="mitodl/data-platform-edx-pipeline",
    ),
]

dagster_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/dagster/",
    ],
)

dagster_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/applications/dagster/",
        "pipelines/infrastructure/scripts/",
    ],
)

container_dependencies = [
    GetStep(get=container_resource.name, trigger=True)
    for container_resource in dagster_container_resources
]

dagster_ami_fragment = packer_jobs(
    dependencies=container_dependencies,
    image_code=dagster_image_code,
    packer_template_path="src/bilder/images/dagster/dagster.pkr.hcl",
    env_vars_from_files={"DAGSTER_VERSION": "dagit/tag"},
)

dagster_pulumi_fragment = pulumi_jobs_chain(
    dagster_pulumi_code,
    stack_names=[f"applications.dagster.{stage}" for stage in ("QA", "Production")],
    project_name="ol-infrastructure-dagster-server",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/dagster/"),
    dependencies=[
        GetStep(
            get=dagster_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[dagster_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=dagster_ami_fragment.resource_types
    + dagster_pulumi_fragment.resource_types,
    resources=dagster_ami_fragment.resources
    + dagster_pulumi_fragment.resources
    + dagster_container_resources,
    jobs=dagster_ami_fragment.jobs + dagster_pulumi_fragment.jobs,
)


dagster_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources + [dagster_image_code, dagster_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(dagster_pipeline.json(indent=2))
    sys.stdout.write(dagster_pipeline.json(indent=2))
    print()
    print("fly -t pr-inf sp -p packer-pulumi-dagster -c definition.json")
