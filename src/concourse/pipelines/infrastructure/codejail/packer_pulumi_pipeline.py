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
    env_vars_from_files={"CODEJAIL_VERSION": "codejail-container/tag"},
)

network_stages = {
    "mitxonline": ("QA", "Production"),
    "mitx": ("CI", "QA", "Production"),
    "mitx-staging": ("CI", "QA", "Production"),
}

pulumi_jobs = []
for network, stages in network_stages.items():
    codejail_pulumi_fragment = pulumi_jobs_chain(
        codejail_pulumi_code,
        stack_names=[f"applications.codejail.{network}.{stage}" for stage in stages],
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
    pulumi_jobs.append(codejail_pulumi_fragment)

combined_fragment = PipelineFragment(
    resource_types=codejail_ami_fragment.resource_types
    + [fragment.resource_types for fragment in pulumi_jobs],
    resources=codejail_ami_fragment.resources
    + [fragment.resources for fragment in pulumi_jobs]
    + [codejail_container_resource],
    jobs=[fragment.jobs for fragment in pulumi_jobs] + codejail_pulumi_fragment.jobs,
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
    print("fly -t pr-inf sp -p packer-pulumi-codejail -c definition.json")
