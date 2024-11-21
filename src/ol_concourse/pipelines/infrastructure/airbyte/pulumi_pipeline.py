from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, github_release
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

airbyte_release = github_release(Identifier("airbyte-release"), "airbytehq", "airbyte")

airbyte_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/airbyte/",
        "src/bridge/secrets/airbyte/",
    ],
)

get_airbyte_release = GetStep(get=airbyte_release.name, trigger=True)

airbyte_pulumi_fragment = pulumi_jobs_chain(
    airbyte_pulumi_code,
    stack_names=[
        f"applications.airbyte.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-airbyte-server",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/airbyte/"),
    dependencies=[get_airbyte_release],
)

combined_fragment = PipelineFragment(
    resource_types=airbyte_pulumi_fragment.resource_types,
    resources=airbyte_pulumi_fragment.resources,
    jobs=airbyte_pulumi_fragment.jobs,
)


airbyte_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        airbyte_release,
        airbyte_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(airbyte_pipeline.model_dump_json(indent=2))
    sys.stdout.write(airbyte_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-airbyte -c definition.json")  # noqa: T201
