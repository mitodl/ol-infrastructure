from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

redash_container_resource = registry_image(
    name=Identifier("redash-container"),
    image_repository="mitodl/redash",
)

redash_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/redash/",
    ],
)

redash_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/redash/",
        "src/bridge/secrets/redash/",
    ],
)

container_dependencies = [GetStep(get=redash_container_resource.name, trigger=True)]

redash_ami_fragment = packer_jobs(
    dependencies=container_dependencies,
    image_code=redash_image_code,
    packer_template_path="src/bilder/images/redash/redash.pkr.hcl",
    env_vars_from_files={"REDASH_VERSION": "redash-container/tag"},
)

redash_pulumi_fragment = pulumi_jobs_chain(
    redash_pulumi_code,
    stack_names=[f"applications.redash.{stage}" for stage in ("QA", "Production")],
    project_name="ol-infrastructure-redash-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/redash/"),
    dependencies=[
        GetStep(
            get=redash_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[redash_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=redash_ami_fragment.resource_types
    + redash_pulumi_fragment.resource_types,
    resources=redash_ami_fragment.resources
    + redash_pulumi_fragment.resources
    + [redash_container_resource],
    jobs=redash_ami_fragment.jobs + redash_pulumi_fragment.jobs,
)


redash_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[*combined_fragment.resources, redash_image_code, redash_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(redash_pipeline.model_dump_json(indent=2))
    sys.stdout.write(redash_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-redash -c definition.json")  # noqa: T201
