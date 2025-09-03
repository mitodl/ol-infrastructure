from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

open_discussions_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-open_discussions"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        *PULUMI_WATCHED_PATHS,
        str(PULUMI_CODE_PATH.joinpath("applications/open_discussions/")),
        "src/bridge/secrets/open_discussions/",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=open_discussions_pulumi_code,
    stack_names=[
        "applications.open_discussions.QA",
        "applications.open_discussions.Production",
    ],
    project_name="ol-infrastructure-open_discussions-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/open_discussions/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
open_discussions_pipeline = Pipeline(
    resources=[*mm_fragment.resources, open_discussions_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(open_discussions_pipeline.json(indent=2))
    sys.stdout.write(open_discussions_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t pr-inf set-pipeline -p pulumi-open-discussions -c definition.json"
    )
