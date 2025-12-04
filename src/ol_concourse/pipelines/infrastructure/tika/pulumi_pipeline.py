from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

tika_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        PULUMI_CODE_PATH.joinpath("applications/tika/"),
    ],
)

tika_pulumi_fragment = pulumi_jobs_chain(
    tika_pulumi_code,
    project_name="ol-infrastructure-tika-server",
    stack_names=[f"applications.tika.{stage}" for stage in ("CI", "QA", "Production")],
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/tika/"),
    dependencies=[],
)

combined_fragment = PipelineFragment(
    resource_types=tika_pulumi_fragment.resource_types,
    resources=[
        *tika_pulumi_fragment.resources,
        tika_pulumi_code,
    ],
    jobs=[
        *tika_pulumi_fragment.jobs,
    ],
)

tika_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources,
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(tika_pipeline.model_dump_json(indent=2))
    sys.stdout.write(tika_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-tika -c definition.json")  # noqa: T201
