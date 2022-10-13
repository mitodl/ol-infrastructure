from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resources import git_repo, registry_image

codejail_container_resource = registry_image(
    name=Identifier("codejail-container"),
    image_repository="mitodl/codejail",
)

codejail_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/codejail/",
    ],
)

codejail_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/applications/codejail/",
        "pipelines/infrastructure/scripts/",
    ],
)

container_dependencies = [GetStep(get=codejail_container_resource.name, trigger=True)]

codejail_ami_fragment = packer_jobs(
    dependencies=container_dependencies,
    image_code=codejail_image_code,
    packer_template_path="src/bilder/images/codejail/codejail.pkr.hcl",
    env_vars_from_files={"codejail_VERSION": "codejail-container/tag"},
)

codejail_pulumi_fragment = pulumi_jobs_chain(
    codejail_pulumi_code,
    stack_names=[f"applications.codejail.{stage}" for stage in ("QA", "Production")],
    project_name="ol-infrastructure-codejail-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/codejail/"),
    dependencies=[
        GetStep(
            get=codejail_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[codejail_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=codejail_ami_fragment.resource_types
    + codejail_pulumi_fragment.resource_types,
    resources=codejail_ami_fragment.resources
    + codejail_pulumi_fragment.resources
    + [codejail_container_resource],
    jobs=codejail_ami_fragment.jobs + codejail_pulumi_fragment.jobs,
)


codejail_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources + [codejail_image_code, codejail_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys  # noqa: WPS433

    with open("definition.json", "w") as definition:
        definition.write(codejail_pipeline.json(indent=2))
    sys.stdout.write(codejail_pipeline.json(indent=2))
    print()  # noqa: WPS421
    print(
        "fly -t pr-inf sp -p packer-pulumi-codejail -c definition.json"
    )  # noqa: WPS421
