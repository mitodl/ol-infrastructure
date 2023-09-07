from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

ocw_studio_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-ocw_studio"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        *PULUMI_WATCHED_PATHS,
        str(PULUMI_CODE_PATH.joinpath("applications/ocw_studio/")),
        "src/bridge/secrets/ocw_studio/",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=ocw_studio_pulumi_code,
    stack_names=[
        "applications.ocw_studio.CI",
        "applications.ocw_studio.QA",
        "applications.ocw_studio.Production",
    ],
    project_name="ol-infrastructure-ocw_studio-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/ocw_studio/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
ocw_studio_pipeline = Pipeline(
    resources=[*mm_fragment.resources, ocw_studio_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ocw_studio_pipeline.json(indent=2))
    sys.stdout.write(ocw_studio_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t pr-inf set-pipeline -p pulumi-ocw_studio -c definition.json"
    )
