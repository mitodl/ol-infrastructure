from ol_concourse.lib.jobs.infrastructure import pulumi_job
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline, Resource
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

app_list = [
    "apps",
    "mitx",
    "mitx-staging",
    "mitxonline",
    "xpro",
    "open",
    "mitopen",
    "celery_monitoring",
]

shared_pulumi_code_resource = git_repo(
    name=Identifier("shared-opensearch-pulumi-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="md/issue_2358",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/infrastructure/aws/opensearch/__main__.py",
    ],
)

local_resources: list[Resource] = []
local_fragments: list[PipelineFragment] = []
for app in app_list:
    for env in ["CI", "QA", "Production"]:
        local_pulumi_code_resource = git_repo(
            name=Identifier(f"pulumi-{app}-{env.lower()}-opensearch-code"),
            uri="https://github.com/mitodl/ol-infrastructure",
            branch="md/issue_2358",
            paths=[
                f"src/ol_infrastructure/infrastructure/aws/opensearch/Pulumi.infrastructure.aws.opensearch.{app}.{env}.yaml"
            ],
        )
        shared_code_get_step = GetStep(
            get=shared_pulumi_code_resource.name, trigger=True
        )
        local_pulumi_fragment = pulumi_job(
            pulumi_code=local_pulumi_code_resource,
            stack_name=f"infrastructure.aws.opensearch.{app}.{env}",
            project_name=f"{app}-{env.lower()}-opensearch-code",
            dependencies=[shared_code_get_step],
            project_source_path=PULUMI_CODE_PATH.joinpath(
                "infrastructure/aws/opensearch/"
            ),
        )
        local_resources.append(local_pulumi_code_resource)
        local_fragments.append(local_pulumi_fragment)

combined_fragments = PipelineFragment.combine_fragments(*local_fragments)
aws_opensearch_pipeline = Pipeline(
    resources=[*local_resources, shared_pulumi_code_resource],
    resource_types=[],
    jobs=combined_fragments.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(aws_opensearch_pipeline.model_dump_json(indent=2))
    sys.stdout.write(aws_opensearch_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-aws-opensearch -c definition.json")  # noqa: T201
