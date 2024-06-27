from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

simple_resource_types = []
simple_resources = []
simple_jobs = []

for service in ["mitx", "mitx-staging", "mitxonline", "xpro"]:
    simple_pulumi_code = git_repo(
        name=Identifier(f"ol-infrastructure-pulumi-{service}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/infrastructure/mongodb_atlas/",
        ],
    )

    if service == "mitxonline":
        stage_list = ["QA", "Production"]
    else:
        stage_list = ["CI", "QA", "Production"]
    simple_pulumi_chain = pulumi_jobs_chain(
        simple_pulumi_code,
        project_name=f"ol-infrastructure-mongodb_atlas-{service}",
        stack_names=[
            f"infrastructure.mongodb_atlas.{service}.{stage}" for stage in stage_list
        ],
        project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/mongodb_atlas/"),
        dependencies=[],
    )

    simple_service_fragment = PipelineFragment(
        resource_types=simple_pulumi_chain.resource_types,
        resources=[simple_pulumi_code, *simple_pulumi_chain.resources],
        jobs=simple_pulumi_chain.jobs,
    )
    simple_resource_types.extend(simple_service_fragment.resource_types)
    simple_resources.extend([simple_pulumi_code, *simple_service_fragment.resources])
    simple_jobs.extend(simple_service_fragment.jobs)


simple_services_combined_fragment = PipelineFragment(
    resource_types=simple_resource_types,
    resources=simple_resources,
    jobs=simple_jobs,
)


mongodb_atlas_pipeline = Pipeline(
    resource_types=simple_services_combined_fragment.resource_types,
    resources=simple_services_combined_fragment.resources,
    jobs=simple_services_combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(mongodb_atlas_pipeline.model_dump_json(indent=2))
    sys.stdout.write(mongodb_atlas_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-mongodb-atlas -c definition.json")  # noqa: T201
