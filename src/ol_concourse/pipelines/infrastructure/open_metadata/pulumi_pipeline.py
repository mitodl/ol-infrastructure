from ol_concourse.lib.jobs.infrastructure import (
    pulumi_jobs_chain,  # noqa: D100, INP001, RUF100
)
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, github_release
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

open_metadata_release = github_release(
    Identifier("open-metadata-release"), "open-metadata", "OpenMetadata"
)

open_metadata_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/open_metadata/",
        "src/bridge/secrets/open_metadata/",
    ],
)

get_open_metadata_release = GetStep(get=open_metadata_release.name, trigger=True)

open_metadata_pulumi_fragment = pulumi_jobs_chain(
    open_metadata_pulumi_code,
    stack_names=[
        f"applications.open_metadata.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-open-metadata-server",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/open_metadata/"),
    dependencies=[get_open_metadata_release],
)

combined_fragment = PipelineFragment(
    resource_types=open_metadata_pulumi_fragment.resource_types,
    resources=open_metadata_pulumi_fragment.resources,
    jobs=open_metadata_pulumi_fragment.jobs,
)


open_metadata_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        open_metadata_release,
        open_metadata_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(open_metadata_pipeline.model_dump_json(indent=2))
    sys.stdout.write(open_metadata_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-open-metadata -c definition.json")  # noqa: T201
