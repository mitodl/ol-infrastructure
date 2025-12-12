from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

app_list = [
    "apps",
    "mitx",
    "mitx-staging",
    "mitxonline",
    "xpro",
    "open",
    "mitlearn",
    "celery_monitoring",
    "open_metadata",
]

shared_pulumi_code_resource = git_repo(
    name=Identifier("ol-infrastructure-pulumiopensearch"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/infrastructure/aws/opensearch/",
    ],
)

local_fragments: list[PipelineFragment] = []
for app in app_list:
    stages = ["CI", "QA", "Production"]
    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code=shared_pulumi_code_resource,
        stack_names=[
            f"infrastructure.aws.opensearch.{app}.{stage}" for stage in stages
        ],
        project_name="ol-infrastructure-opensearch",
        dependencies=[],
        project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/aws/opensearch/"),
    )
    local_fragments.append(pulumi_fragment)

combined_fragments = PipelineFragment.combine_fragments(*local_fragments)
aws_opensearch_pipeline = Pipeline(
    resources=[*combined_fragments.resources, shared_pulumi_code_resource],
    resource_types=combined_fragments.resource_types,
    jobs=combined_fragments.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(aws_opensearch_pipeline.model_dump_json(indent=2))
    sys.stdout.write(aws_opensearch_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-aws-opensearch -c definition.json")  # noqa: T201
