from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

# Simple services that follow well defined patterns (CI -> QA -> Production)
simple_resource_types = []
simple_resources = []
simple_jobs = []
for service in ["kms", "network"]:
    simple_pulumi_code = git_repo(
        name=Identifier(f"ol-infrastructure-pulumi-{service}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            f"src/ol_infrastructure/infrastructure/aws/{service}",
        ],
    )

    simple_pulumi_chain = pulumi_jobs_chain(
        simple_pulumi_code,
        project_name=f"ol-infrastructure-aws-{service}",
        stack_names=[
            f"infrastructure.aws.{service}.{stage}"
            for stage in ("CI", "QA", "Production")
        ],
        project_source_path=PULUMI_CODE_PATH.joinpath(f"infrastructure/aws/{service}/"),
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

# One off services that only exist in stack (no stage)
oneoff_resource_types = []
oneoff_resources = []
oneoff_jobs = []
for service in ["dns", "policies", "iam"]:
    oneoff_pulumi_code = git_repo(
        name=Identifier(f"ol-infrastructure-pulumi-{service}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            f"src/ol_infrastructure/infrastructurre/aws/{service}",
        ],
    )

    oneoff_pulumi_chain = pulumi_jobs_chain(
        oneoff_pulumi_code,
        project_name=f"ol-infrastructure-aws-{service}",
        stack_names=[f"infrastructure.aws.{service}"],
        project_source_path=PULUMI_CODE_PATH.joinpath(f"infrastructure/aws/{service}/"),
        dependencies=[],
    )

    oneoff_service_fragment = PipelineFragment(
        resource_types=oneoff_pulumi_chain.resource_types,
        resources=[oneoff_pulumi_code, *oneoff_pulumi_chain.resources],
        jobs=oneoff_pulumi_chain.jobs,
    )
    oneoff_resource_types.extend(oneoff_service_fragment.resource_types)
    oneoff_resources.extend([oneoff_pulumi_code, *oneoff_service_fragment.resources])
    oneoff_jobs.extend(oneoff_service_fragment.jobs)

oneoff_services_combined_fragment = PipelineFragment(
    resource_types=oneoff_resource_types,
    resources=oneoff_resources,
    jobs=oneoff_jobs,
)

fully_combined_fragment = PipelineFragment(
    resource_types=oneoff_services_combined_fragment.resource_types
    + simple_services_combined_fragment.resource_types,
    resources=oneoff_services_combined_fragment.resources
    + simple_services_combined_fragment.resources,
    jobs=oneoff_services_combined_fragment.jobs
    + simple_services_combined_fragment.jobs,
)

aws_pipeline = Pipeline(
    resource_types=fully_combined_fragment.resource_types,
    resources=fully_combined_fragment.resources,
    jobs=fully_combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(aws_pipeline.model_dump_json(indent=2))
    sys.stdout.write(aws_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-aws -c definition.json")  # noqa: T201
