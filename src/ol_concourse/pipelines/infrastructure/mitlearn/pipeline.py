from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

mitlearn_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-mitlearn"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/mitlearn/",
        "src/bridge/lib/",
        *PULUMI_WATCHED_PATHS,
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=mitlearn_pulumi_code,
    stack_names=[
        "applications.mitlearn.QA",
        "applications.mitlearn.Production",
    ],
    project_name="ol-infrastructure-mitlearn-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/mitlearn/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
mitlearn_pipeline = Pipeline(
    resources=[*mm_fragment.resources, mitlearn_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(mitlearn_pipeline.json(indent=2))
    sys.stdout.write(mitlearn_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write("fly -t pr-inf set-pipeline -p pulumi-mitlearn -c definition.json")
