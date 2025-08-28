from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

fastly_redirector_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/fastly_redirector/",
    ],
)

fastly_redirector_pulumi_fragment = pulumi_jobs_chain(
    fastly_redirector_pulumi_code,
    stack_names=[
        f"applications.fastly_redirector.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-fastly-redirector",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/fastly_redirector/"),
)

combined_fragment = PipelineFragment(
    resource_types=fastly_redirector_pulumi_fragment.resource_types,
    resources=fastly_redirector_pulumi_fragment.resources,
    jobs=fastly_redirector_pulumi_fragment.jobs,
)


fastly_redirector_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        fastly_redirector_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(fastly_redirector_pipeline.model_dump_json(indent=2))
    sys.stdout.write(fastly_redirector_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-fastly-redirector -c definition.json")  # noqa: T201
